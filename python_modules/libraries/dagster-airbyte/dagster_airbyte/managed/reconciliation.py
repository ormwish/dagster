from typing import Any, Callable, Dict, Iterable, List, Mapping, Optional, Tuple, cast

from dagster_airbyte.asset_defs import (
    AirbyteConnectionMetadata,
    AirbyteInstanceCacheableAssetsDefintion,
    _clean_name,
)
from dagster_airbyte.managed.types import (
    AirbyteConnection,
    AirbyteDestination,
    AirbyteSource,
    AirbyteSyncMode,
    InitializedAirbyteConnection,
    InitializedAirbyteDestination,
    InitializedAirbyteSource,
)
from dagster_airbyte.resources import AirbyteResource
from dagster_airbyte.utils import is_basic_normalization_operation
from dagster_managed_elements import (
    ManagedElementCheckResult,
    ManagedElementDiff,
    ManagedElementError,
)
from dagster_managed_elements.types import ManagedElementReconciler
from dagster_managed_elements.utils import diff_dicts

import dagster._check as check
from dagster import ResourceDefinition
from dagster._annotations import experimental
from dagster._core.definitions.cacheable_assets import CacheableAssetsDefinition
from dagster._core.definitions.events import CoercibleToAssetKeyPrefix
from dagster._core.execution.context.init import build_init_resource_context
from dagster._utils.merger import deep_merge_dicts


def gen_configured_stream_json(
    source_stream: Dict[str, Any], user_stream_config: Dict[str, AirbyteSyncMode]
) -> Dict[str, Any]:
    """
    Generates an Airbyte API stream defintiion based on the succinct user-provided config and the
    full stream definition from the source.
    """
    config = user_stream_config[source_stream["stream"]["name"]]
    return deep_merge_dicts(
        source_stream,
        {"config": {"syncMode": config.value[0], "destinationSyncMode": config.value[1]}},
    )


def diff_sources(
    config_src: Optional[AirbyteSource], curr_src: Optional[AirbyteSource]
) -> ManagedElementCheckResult:
    """
    Utility to diff two AirbyteSource objects.
    """
    diff = diff_dicts(
        config_src.source_configuration if config_src else {},
        curr_src.source_configuration if curr_src else {},
    )
    if not diff.is_empty():
        name = config_src.name if config_src else curr_src.name if curr_src else "Unknown"
        return ManagedElementDiff().with_nested(name, diff)

    return ManagedElementDiff()


def diff_destinations(
    config_dst: Optional[AirbyteDestination], curr_dst: Optional[AirbyteDestination]
) -> ManagedElementCheckResult:
    """
    Utility to diff two AirbyteDestination objects.
    """
    diff = diff_dicts(
        config_dst.destination_configuration if config_dst else {},
        curr_dst.destination_configuration if curr_dst else {},
    )
    if not diff.is_empty():
        name = config_dst.name if config_dst else curr_dst.name if curr_dst else "Unknown"
        return ManagedElementDiff().with_nested(name, diff)

    return ManagedElementDiff()


def conn_dict(conn: Optional[AirbyteConnection]) -> Dict[str, Any]:
    if not conn:
        return {}
    return {
        "source": conn.source.name if conn.source else "Unknown",
        "destination": conn.destination.name if conn.destination else "Unknown",
        "normalize data": conn.normalize_data,
        "streams": {k: v.name for k, v in conn.stream_config.items()},
    }


def diff_connections(
    config_conn: Optional[AirbyteConnection], curr_conn: Optional[AirbyteConnection]
) -> ManagedElementCheckResult:
    """
    Utility to diff two AirbyteConnection objects.
    """
    diff = diff_dicts(conn_dict(config_conn), conn_dict(curr_conn))
    if not diff.is_empty():
        name = config_conn.name if config_conn else curr_conn.name if curr_conn else "Unknown"
        return ManagedElementDiff().with_nested(name, diff)

    return ManagedElementDiff()


def reconcile_sources(
    res: AirbyteResource,
    config_sources: Mapping[str, AirbyteSource],
    existing_sources: Mapping[str, InitializedAirbyteSource],
    workspace_id: str,
    dry_run: bool,
    should_delete: bool,
) -> Tuple[Mapping[str, InitializedAirbyteSource], ManagedElementCheckResult]:
    """
    Generates a diff of the configured and existing sources and reconciles them to match the
    configured state if dry_run is False.
    """

    diff = ManagedElementDiff()

    initialized_sources = {}
    for source_name in set(config_sources.keys()).union(existing_sources.keys()):
        configured_source = config_sources.get(source_name)
        existing_source = existing_sources.get(source_name)

        # Ignore sources not mentioned in the user config unless the user specifies to delete
        if not should_delete and existing_source and not configured_source:
            initialized_sources[source_name] = existing_source
            continue

        diff = diff.join(
            diff_sources(configured_source, existing_source.source if existing_source else None)
        )

        if existing_source and (
            not configured_source or (configured_source.must_be_recreated(existing_source.source))
        ):
            initialized_sources[source_name] = existing_source
            if not dry_run:
                res.make_request(
                    endpoint="/sources/delete",
                    data={"sourceId": existing_source.source_id},
                )
            existing_source = None

        if configured_source:
            defn_id = check.not_none(
                res.get_source_definition_by_name(configured_source.source_type, workspace_id)
            )
            base_source_defn_dict = {
                "name": configured_source.name,
                "connectionConfiguration": configured_source.source_configuration,
            }
            source_id = ""
            if existing_source:
                source_id = existing_source.source_id
                if not dry_run:
                    res.make_request(
                        endpoint="/sources/update",
                        data={"sourceId": source_id, **base_source_defn_dict},
                    )
            else:
                if not dry_run:
                    create_result = cast(
                        Dict[str, str],
                        check.not_none(
                            res.make_request(
                                endpoint="/sources/create",
                                data={
                                    "sourceDefinitionId": defn_id,
                                    "workspaceId": workspace_id,
                                    **base_source_defn_dict,
                                },
                            )
                        ),
                    )
                    source_id = create_result["sourceId"]

            initialized_sources[source_name] = InitializedAirbyteSource(
                source=configured_source,
                source_id=source_id,
                source_definition_id=defn_id,
            )

    return initialized_sources, diff


def reconcile_destinations(
    res: AirbyteResource,
    config_destinations: Mapping[str, AirbyteDestination],
    existing_destinations: Mapping[str, InitializedAirbyteDestination],
    workspace_id: str,
    dry_run: bool,
    should_delete: bool,
) -> Tuple[Mapping[str, InitializedAirbyteDestination], ManagedElementCheckResult]:
    """
    Generates a diff of the configured and existing destinations and reconciles them to match the
    configured state if dry_run is False.
    """

    diff = ManagedElementDiff()

    initialized_destinations = {}
    for destination_name in set(config_destinations.keys()).union(existing_destinations.keys()):
        configured_destination = config_destinations.get(destination_name)
        existing_destination = existing_destinations.get(destination_name)

        # Ignore destinations not mentioned in the user config unless the user specifies to delete
        if not should_delete and existing_destination and not configured_destination:
            initialized_destinations[destination_name] = existing_destination
            continue

        diff = diff.join(
            diff_destinations(
                configured_destination,
                existing_destination.destination if existing_destination else None,
            )
        )

        if existing_destination and (
            not configured_destination
            or (configured_destination.must_be_recreated(existing_destination.destination))
        ):
            initialized_destinations[destination_name] = existing_destination
            if not dry_run:
                res.make_request(
                    endpoint="/destinations/delete",
                    data={"destinationId": existing_destination.destination_id},
                )
        elif configured_destination:
            defn_id = res.get_destination_definition_by_name(
                configured_destination.destination_type, workspace_id
            )
            base_destination_defn_dict = {
                "name": configured_destination.name,
                "connectionConfiguration": configured_destination.destination_configuration,
            }
            destination_id = ""
            if existing_destination:
                destination_id = existing_destination.destination_id
                if not dry_run:
                    res.make_request(
                        endpoint="/destinations/update",
                        data={"destinationId": destination_id, **base_destination_defn_dict},
                    )
            else:
                if not dry_run:
                    create_result = cast(
                        Dict[str, str],
                        check.not_none(
                            res.make_request(
                                endpoint="/destinations/create",
                                data={
                                    "destinationDefinitionId": defn_id,
                                    "workspaceId": workspace_id,
                                    **base_destination_defn_dict,
                                },
                            )
                        ),
                    )
                    destination_id = create_result["destinationId"]

            initialized_destinations[destination_name] = InitializedAirbyteDestination(
                destination=configured_destination,
                destination_id=destination_id,
                destination_definition_id=defn_id,
            )

    return initialized_destinations, diff


def reconcile_config(
    res: AirbyteResource,
    objects: List[AirbyteConnection],
    dry_run: bool = False,
    should_delete: bool = False,
) -> ManagedElementCheckResult:
    """
    Main entry point for the reconciliation process. Takes a list of AirbyteConnection objects
    and a pointer to an Airbyte instance and returns a diff, along with applying the diff
    if dry_run is False.
    """
    with res.cache_requests():
        config_connections = {conn.name: conn for conn in objects}
        config_sources = {conn.source.name: conn.source for conn in objects}
        config_dests = {conn.destination.name: conn.destination for conn in objects}

        workspace_id = res.get_default_workspace()

        existing_sources_raw = cast(
            Dict[str, List[Dict[str, Any]]],
            check.not_none(
                res.make_request(endpoint="/sources/list", data={"workspaceId": workspace_id})
            ),
        )
        existing_dests_raw = cast(
            Dict[str, List[Dict[str, Any]]],
            check.not_none(
                res.make_request(endpoint="/destinations/list", data={"workspaceId": workspace_id})
            ),
        )

        existing_sources: Dict[str, InitializedAirbyteSource] = {
            source_json["name"]: InitializedAirbyteSource.from_api_json(source_json)
            for source_json in existing_sources_raw.get("sources", [])
        }
        existing_dests: Dict[str, InitializedAirbyteDestination] = {
            destination_json["name"]: InitializedAirbyteDestination.from_api_json(destination_json)
            for destination_json in existing_dests_raw.get("destinations", [])
        }

        # First, remove any connections that need to be deleted, so that we can
        # safely delete any sources/destinations that are no longer referenced
        # or that need to be recreated.
        connections_diff = reconcile_connections_pre(
            res,
            config_connections,
            existing_sources,
            existing_dests,
            workspace_id,
            dry_run,
            should_delete,
        )

        all_sources, sources_diff = reconcile_sources(
            res, config_sources, existing_sources, workspace_id, dry_run, should_delete
        )
        all_dests, dests_diff = reconcile_destinations(
            res, config_dests, existing_dests, workspace_id, dry_run, should_delete
        )

        # Now that we have updated the set of sources and destinations, we can
        # recreate or update any connections which depend on them.
        reconcile_connections_post(
            res,
            config_connections,
            all_sources,
            all_dests,
            workspace_id,
            dry_run,
        )

        return ManagedElementDiff().join(sources_diff).join(dests_diff).join(connections_diff)


def reconcile_normalization(
    res: AirbyteResource,
    existing_connection_id: Optional[str],
    destination: InitializedAirbyteDestination,
    normalization_config: Optional[bool],
    workspace_id: str,
) -> Optional[str]:
    """
    Reconciles the normalization configuration for a connection.

    If normalization_config is None, then defaults to True on destinations that support normalization
    and False on destinations that do not.
    """
    existing_basic_norm_op_id = None
    if existing_connection_id:
        operations = cast(
            Dict[str, List[Dict[str, str]]],
            check.not_none(
                res.make_request(
                    endpoint="/operations/list",
                    data={"connectionId": existing_connection_id},
                )
            ),
        )
        existing_basic_norm_op = next(
            (
                operation
                for operation in operations["operations"]
                if is_basic_normalization_operation(operation)
            ),
            None,
        )
        existing_basic_norm_op_id = (
            existing_basic_norm_op["operationId"] if existing_basic_norm_op else None
        )

    if normalization_config is not False:
        if destination.destination_definition_id and res.does_dest_support_normalization(
            destination.destination_definition_id, workspace_id
        ):
            if existing_basic_norm_op_id:
                return existing_basic_norm_op_id
            else:
                return cast(
                    Dict[str, str],
                    check.not_none(
                        res.make_request(
                            endpoint="/operations/create",
                            data={
                                "workspaceId": workspace_id,
                                "name": "Normalization",
                                "operatorConfiguration": {
                                    "operatorType": "normalization",
                                    "normalization": {"option": "basic"},
                                },
                            },
                        )
                    ),
                )["operationId"]
        elif normalization_config is True:
            raise Exception(
                f"Destination {destination.destination.name} does not support normalization."
            )

    return None


def reconcile_connections_pre(
    res: AirbyteResource,
    config_connections: Mapping[str, AirbyteConnection],
    existing_sources: Mapping[str, InitializedAirbyteSource],
    existing_destinations: Mapping[str, InitializedAirbyteDestination],
    workspace_id: str,
    dry_run: bool,
    should_delete: bool,
) -> ManagedElementCheckResult:
    """
    Generates the diff for connections, and deletes any connections that are not in the config if
    dry_run is False.

    It's necessary to do this in two steps because we need to remove connections that depend on
    sources and destinations that are being deleted or recreated before Airbyte will allow us to
    delete or recreate them.
    """

    diff = ManagedElementDiff()

    existing_connections_raw = cast(
        Dict[str, List[Dict[str, Any]]],
        check.not_none(
            res.make_request(endpoint="/connections/list", data={"workspaceId": workspace_id})
        ),
    )
    existing_connections: Dict[str, InitializedAirbyteConnection] = {
        connection_json["name"]: InitializedAirbyteConnection.from_api_json(
            connection_json, existing_sources, existing_destinations
        )
        for connection_json in existing_connections_raw.get("connections", [])
    }

    for conn_name in set(config_connections.keys()).union(existing_connections.keys()):
        config_conn = config_connections.get(conn_name)
        existing_conn = existing_connections.get(conn_name)

        # Ignore connections not mentioned in the user config unless the user specifies to delete
        if not should_delete and not config_conn:
            continue

        diff = diff.join(
            diff_connections(config_conn, existing_conn.connection if existing_conn else None)
        )

        if existing_conn and (
            not config_conn or config_conn.must_be_recreated(existing_conn.connection)
        ):
            if not dry_run:
                res.make_request(
                    endpoint="/connections/delete",
                    data={"connectionId": existing_conn.connection_id},
                )
    return diff


def reconcile_connections_post(
    res: AirbyteResource,
    config_connections: Mapping[str, AirbyteConnection],
    init_sources: Mapping[str, InitializedAirbyteSource],
    init_dests: Mapping[str, InitializedAirbyteDestination],
    workspace_id: str,
    dry_run: bool,
) -> None:
    """
    Creates new and modifies existing connections based on the config if dry_run is False.
    """

    existing_connections_raw = cast(
        Dict[str, List[Dict[str, Any]]],
        check.not_none(
            res.make_request(endpoint="/connections/list", data={"workspaceId": workspace_id})
        ),
    )
    existing_connections = {
        connection_json["name"]: InitializedAirbyteConnection.from_api_json(
            connection_json, init_sources, init_dests
        )
        for connection_json in existing_connections_raw.get("connections", [])
    }

    for conn_name, config_conn in config_connections.items():
        existing_conn = existing_connections.get(conn_name)

        normalization_operation_id = None
        if not dry_run:
            destination = init_dests[config_conn.destination.name]

            # Enable or disable basic normalization based on config
            normalization_operation_id = reconcile_normalization(
                res,
                existing_connections.get("name", {}).get("connectionId"),
                destination,
                config_conn.normalize_data,
                workspace_id,
            )

        configured_streams = []
        if not dry_run:
            source = init_sources[config_conn.source.name]
            schema = res.get_source_schema(source.source_id)
            base_streams = schema["catalog"]["streams"]

            configured_streams = [
                gen_configured_stream_json(stream, config_conn.stream_config)
                for stream in base_streams
                if stream["stream"]["name"] in config_conn.stream_config
            ]

        connection_base_json = {
            "name": conn_name,
            "namespaceDefinition": "source",
            "namespaceFormat": "${SOURCE_NAMESPACE}",
            "prefix": "",
            "operationIds": [normalization_operation_id] if normalization_operation_id else [],
            "syncCatalog": {"streams": configured_streams},
            "scheduleType": "manual",
            "status": "active",
        }

        if existing_conn:
            if not dry_run:
                source = init_sources[conn_name]
                res.make_request(
                    endpoint="/connections/update",
                    data={
                        **connection_base_json,
                        "sourceCatalogId": res.get_source_catalog_id(source.source_id),
                        "connectionId": existing_conn.connection_id,
                    },
                )
        else:
            if not dry_run:
                source = init_sources[config_conn.source.name]
                destination = init_dests[config_conn.destination.name]
                res.make_request(
                    endpoint="/connections/create",
                    data={
                        **connection_base_json,
                        "sourceCatalogId": res.get_source_catalog_id(source.source_id),
                        "sourceId": source.source_id,
                        "destinationId": destination.destination_id,
                    },
                )


@experimental
class AirbyteManagedElementReconciler(ManagedElementReconciler):
    def __init__(
        self,
        airbyte: ResourceDefinition,
        connections: Iterable[AirbyteConnection],
        delete_unmented_resources: bool = False,
    ):
        """
        Reconciles Python-specified Airbyte resources with an Airbyte instance.

        Args:
            airbyte (ResourceDefinition): The Airbyte resource definition to reconcile against.
            connections (Iterable[AirbyteConnection]): The Airbyte connection objects to reconcile.
            delete_unmented_resources (bool): Whether to delete resources that are not mentioned in
                the set of connections provided. When True, all Airbyte instance contents are effectively
                managed by the reconciler. Defaults to False.
        """
        airbyte = check.inst_param(airbyte, "airbyte", ResourceDefinition)

        self._airbyte_instance: AirbyteResource = airbyte(build_init_resource_context())
        self._connections = list(
            check.iterable_param(connections, "connections", of_type=AirbyteConnection)
        )
        self._delete_unmentioned_resources = check.bool_param(
            delete_unmented_resources, "delete_unmented_resources"
        )

        super().__init__()

    def check(self) -> ManagedElementCheckResult:
        return reconcile_config(
            self._airbyte_instance,
            self._connections,
            dry_run=True,
            should_delete=self._delete_unmentioned_resources,
        )

    def apply(self) -> ManagedElementCheckResult:
        return reconcile_config(
            self._airbyte_instance,
            self._connections,
            dry_run=False,
            should_delete=self._delete_unmentioned_resources,
        )


class AirbyteManagedElementCacheableAssetsDefinition(AirbyteInstanceCacheableAssetsDefintion):
    def __init__(
        self,
        airbyte_resource_def: ResourceDefinition,
        key_prefix: List[str],
        create_assets_for_normalization_tables: bool,
        connection_to_group_fn: Optional[Callable[[str], Optional[str]]],
        connections: Iterable[AirbyteConnection],
    ):
        super().__init__(
            airbyte_resource_def=airbyte_resource_def,
            workspace_id=None,
            key_prefix=key_prefix,
            create_assets_for_normalization_tables=create_assets_for_normalization_tables,
            connection_to_group_fn=connection_to_group_fn,
            connection_filter=None,
        )
        self._connections: List[AirbyteConnection] = list(connections)

    def _get_connections(self) -> List[Tuple[str, AirbyteConnectionMetadata]]:
        diff = reconcile_config(self._airbyte_instance, self._connections, dry_run=True)
        if isinstance(diff, ManagedElementDiff) and not diff.is_empty():
            raise ValueError(
                "Airbyte connections are not in sync with provided configuration, diff:\n{}".format(
                    str(diff)
                )
            )
        elif isinstance(diff, ManagedElementError):
            raise ValueError("Error checking Airbyte connections: {}".format(str(diff)))

        return super()._get_connections()


@experimental
def load_assets_from_connections(
    airbyte: ResourceDefinition,
    connections: Iterable[AirbyteConnection],
    key_prefix: Optional[CoercibleToAssetKeyPrefix] = None,
    create_assets_for_normalization_tables: bool = True,
    connection_to_group_fn: Optional[Callable[[str], Optional[str]]] = _clean_name,
) -> CacheableAssetsDefinition:
    """
    Loads Airbyte connection assets from a configured AirbyteResource instance, checking against a list of AirbyteConnection objects.

    Args:
        airbyte (ResourceDefinition): An AirbyteResource configured with the appropriate connection
            details.
        connections (Iterable[AirbyteConnection]): A list of AirbyteConnection objects to build assets for.
        key_prefix (Optional[CoercibleToAssetKeyPrefix]): A prefix for the asset keys created.
        create_assets_for_normalization_tables (bool): If True, assets will be created for tables
            created by Airbyte's normalization feature. If False, only the destination tables
            will be created. Defaults to True.
        connection_to_group_fn (Optional[Callable[[str], Optional[str]]]): Function which returns an asset
            group name for a given Airbyte connection name. If None, no groups will be created. Defaults
            to a basic sanitization function.

    """

    if isinstance(key_prefix, str):
        key_prefix = [key_prefix]
    key_prefix = check.list_param(key_prefix or [], "key_prefix", of_type=str)

    return AirbyteManagedElementCacheableAssetsDefinition(
        airbyte_resource_def=check.inst_param(airbyte, "airbyte", ResourceDefinition),
        key_prefix=key_prefix,
        create_assets_for_normalization_tables=check.bool_param(
            create_assets_for_normalization_tables, "create_assets_for_normalization_tables"
        ),
        connection_to_group_fn=check.opt_callable_param(
            connection_to_group_fn, "connection_to_group_fn"
        ),
        connections=check.iterable_param(connections, "connections", of_type=AirbyteConnection),
    )