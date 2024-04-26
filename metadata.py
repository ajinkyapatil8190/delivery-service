import collections.abc
import dataclasses
import datetime

import dacite
import falcon
import falcon.media.validators.jsonschema
import sqlalchemy as sa
import sqlalchemy.orm.session as ss

import delivery.model
import dso.model
import gci.componentmodel as cm

import compliance_summary as cs
import deliverydb.model as dm
import deliverydb.util as du
import eol
import features
import middleware.auth
import paths
import rescore
import util


@middleware.auth.noauth
class ArtefactMetadata:
    required_features = (features.FeatureDeliveryDB,)

    def __init__(
        self,
        eol_client: eol.EolClient,
        artefact_metadata_cfg_by_type: dict,
    ):
        self.eol_client = eol_client
        self.artefact_metadata_cfg_by_type = artefact_metadata_cfg_by_type

    @falcon.media.validators.jsonschema.validate(
        req_schema=util.load_dict_from_yaml(paths.compliance_data_req_jsonschema_path),
        # resp_schema=util.load_dict_from_yaml(paths.compliance_data_resp_jsonschema_path),
    )
    def on_post_query(self, req: falcon.Request, resp: falcon.Response):
        '''
        query artefact-metadata from delivery-db and mix-in existing rescorings

        **expected body:**

            - components: <array> of <object> \n
                - componentName: <str> \n
                - componentVersion: <str> \n
        '''
        body = req.media
        component_filter: list[dict] = body.get('components')
        component_ids = tuple(
            cm.ComponentIdentity(
                name=component.get('componentName'),
                version=component.get('componentVersion'),
            ) for component in component_filter
        )

        session: ss.Session = req.context.db_session

        type_filter = req.get_param_as_list('type', required=False)

        findings_query = session.query(dm.ArtefactMetaData)
        rescorings_query = session.query(dm.ArtefactMetaData).filter(
            dm.ArtefactMetaData.type == dso.model.Datatype.RESCORING,
        )

        if type_filter:
            findings_query = findings_query.filter(
                dm.ArtefactMetaData.type.in_(type_filter),
            )
            rescorings_query = rescorings_query.filter(
                du.ArtefactMetadataFilters.filter_for_rescoring_type(type_filter)
            )

        if component_filter:
            findings_query = findings_query.filter(
                sa.or_(du.ArtefactMetadataQueries.component_queries(
                    components=component_ids,
                )),
            )
            rescorings_query = rescorings_query.filter(
                sa.or_(du.ArtefactMetadataQueries.component_queries(
                    components=component_ids,
                    none_ok=True,
                ))
            )

        findings_raw = findings_query.all()

        rescorings_raw = rescorings_query.all()
        rescorings = tuple(
            delivery.model.ArtefactMetadata.from_dict(
                raw=du.db_artefact_metadata_to_dict(raw),
            ).to_dso_model_artefact_metadata()
            for raw in rescorings_raw
        )

        def iter_findings(
            findings: list[dm.ArtefactMetaData],
            rescorings: tuple[dso.model.ArtefactMetadata],
            artefact_metadata_cfg_by_type: dict[str, cs.ArtefactMetadataCfg],
        ) -> collections.abc.Generator[dict, None, None]:
            def result_dict(
                finding: dm.ArtefactMetaData,
                rescorings: tuple[dso.model.ArtefactMetadata],
                meta: dict=None,
            ) -> dict:
                finding_dict = du.db_artefact_metadata_to_dict(
                    artefact_metadata=finding,
                )

                if rescorings:
                    finding_dict['rescorings'] = rescorings

                if meta:
                    finding_dict['meta'] = meta

                return finding_dict

            for finding in findings:
                cfg = artefact_metadata_cfg_by_type.get(finding.type)

                rescorings_for_finding = rescore.rescorings_for_finding_by_specificity(
                    finding=finding,
                    rescorings=rescorings,
                )

                if not cfg:
                    yield result_dict(
                        finding=finding,
                        rescorings=rescorings_for_finding,
                    )
                    continue

                severity = cs.severity_for_finding(
                    finding=finding,
                    artefact_metadata_cfg=cfg,
                    eol_client=self.eol_client,
                )
                if not severity:
                    yield result_dict(
                        finding=finding,
                        rescorings=rescorings_for_finding,
                    )
                    continue

                yield result_dict(
                    finding=finding,
                    rescorings=rescorings_for_finding,
                    meta=dict(**finding.meta, severity=severity),
                )

        resp.media = list(iter_findings(
            findings=findings_raw,
            rescorings=rescorings,
            artefact_metadata_cfg_by_type=self.artefact_metadata_cfg_by_type,
        ))

    def on_post(self, req: falcon.Request, resp: falcon.Response):
        '''
        store artefact-metadata in delivery-db

        only one database tuple per artefact and artefact-metadata-type is kept, on insert exiting
        entry is overwritten.

        **expected body:**

            - entries: <array> \n
                - artefact: <object> \n
                    - component_name: <str> \n
                    - component_version: <str> \n
                    - artefact: <object> \n
                        - artefact_name: <str> \n
                        - artefact_version: <str> \n
                        - artefact_type: <str> \n
                        - artefact_extra_id: <object>\n
                - meta: <object> \n
                    - type: <str> \n
                    - datasource: <str> \n
                - data: <object> # schema depends on meta.type \n
                - discovery_date: <str of format YYYY-MM-DD> \n
        '''
        body = req.media
        entries: list[dict] = body.get('entries')

        session: ss.Session = req.context.db_session

        type_hooks = {
            datetime.date:
            lambda date: datetime.datetime.strptime(date, '%Y-%m-%d').date() if date else None,
        }

        try:
            for entry in entries:
                entry = _fill_default_values(entry)

                local_artefact_metadata = dacite.from_dict(
                    data_class=dso.model.ArtefactMetadata,
                    data=entry,
                    config=dacite.Config(
                        type_hooks=type_hooks,
                    ),
                )

                new_metadata = du.to_db_artefact_metadata(
                    artefact_metadata=local_artefact_metadata,
                )

                # only keep latest metadata (purge all existing entries)
                session.query(dm.ArtefactMetaData).filter(
                    du.ArtefactMetadataFilters.by_artefact_id_and_type(new_metadata)
                ).delete()

                session.add(new_metadata)

                session.commit()
        except:
            session.rollback()
            raise

        resp.status = falcon.HTTP_CREATED # pylint: disable=E1101

    def on_put(self, req: falcon.Request, resp: falcon.Response):
        '''
        update artefact-metadata in delivery-db

        only the data from the supplied request body is kept, other database tuples for the
        same ocm resource (component-id + resource-id) and type are removed

        **expected body:**

            - entries: <array> \n
                - artefact: <object> \n
                    - component_name: <str> \n
                    - component_version: <str> \n
                    - artefact: <object> \n
                        - artefact_name: <str> \n
                        - artefact_version: <str> \n
                        - artefact_type: <str> \n
                        - artefact_extra_id: <object> \n
                - meta: <object> \n
                    - type: <str> \n
                    - datasource: <str> \n
                - data: <object> # schema depends on meta.type \n
                - discovery_date: <str of format YYYY-MM-DD> \n
        '''
        body = req.media
        entries: list[dict] = body.get('entries')

        session: ss.Session = req.context.db_session
        now = datetime.datetime.now()

        type_hooks = {
            datetime.date:
            lambda date: datetime.datetime.strptime(date, '%Y-%m-%d').date() if date else None,
        }

        existing_artefacts: dict[frozenset, list[dm.ArtefactMetaData]] = dict()
        updated_artefacts: list[dm.ArtefactMetaData] = []
        seen_ocm_resources_for_type = set()

        try:
            for entry in entries:
                entry = _fill_default_values(entry)

                metadata_entry = du.to_db_artefact_metadata(
                    artefact_metadata=dacite.from_dict(
                        data_class=dso.model.ArtefactMetadata,
                        data=entry,
                        config=dacite.Config(type_hooks=type_hooks),
                    )
                )

                resource_key = {
                    'component_name': metadata_entry.component_name,
                    'artefact_name': metadata_entry.artefact_name,
                    'type': metadata_entry.type,
                }
                if metadata_entry.type == dso.model.Datatype.COMPLIANCE_SNAPSHOTS:
                    resource_key['cfg_name'] = metadata_entry.data.get('cfg_name')
                key = frozenset([(k, v) for k, v in resource_key.items()])

                # retrieve existing artefacts only filtered by name and type to reduce db queries
                # as artefacts in different versions might be required later to determine correct
                # discovery date
                if key not in existing_artefacts:
                    existing_entries = session.query(dm.ArtefactMetaData).filter(
                        du.ArtefactMetadataFilters.by_name_and_type(metadata_entry)
                    ).all()
                    existing_artefacts[key] = existing_entries
                else:
                    existing_entries = existing_artefacts[key]

                # add further attributes to key to track which exact ocm resources are part of this
                # request -> required to remove old metadata for this exact ocm resource later
                resource_key.update({
                    'component_version': metadata_entry.component_version,
                    'artefact_version': metadata_entry.artefact_version,
                    'artefact_kind': metadata_entry.artefact_kind,
                    'artefact_type': metadata_entry.artefact_type,
                    # do not include extra id (yet) because there is only one entry for
                    # all ocm resources with different extra ids at the moment
                    # TODO include extra id as soon as there is one entry for each extra id
                    # 'artefact_extra_id': metadata_entry.artefact_extra_id_normalised,
                })
                key = frozenset([(k, v) for k, v in resource_key.items()])
                seen_ocm_resources_for_type.add(key)

                entry_already_exists = False
                reusable_discovery_date = None
                for existing_entry in updated_artefacts + existing_entries:
                    if (metadata_entry.component_name != existing_entry.component_name or
                        metadata_entry.artefact_name != existing_entry.artefact_name or
                        metadata_entry.type != existing_entry.type):
                        continue

                    # if the version of the existing entry does not match the version of the new
                    # finding but in general it's the same finding (e.g. same CVE in same package,
                    # license, ...), we must use its discovery date for the new finding as well
                    if (metadata_entry.component_version != existing_entry.component_version or
                        metadata_entry.artefact_version != existing_entry.artefact_version):
                        if not reusable_discovery_date:
                            reusable_discovery_date = reuse_discovery_date_if_possible(
                                old_metadata=existing_entry,
                                new_metadata=metadata_entry,
                            )
                        continue

                    if entry_already_exists := check_if_findigs_are_equal(
                        old_metadata=existing_entry,
                        new_metadata=metadata_entry,
                    ):
                        # same db entry already exists, skipping new entry and do not remove later
                        if existing_entry in existing_entries:
                            existing_entries.remove(existing_entry)
                        # for compliance snapshots:
                        # update state changes in-place instead of creating new entry
                        if existing_entry.type == dso.model.Datatype.COMPLIANCE_SNAPSHOTS:
                            existing_entry.data = metadata_entry.data

                        # patch in vulnerability summary to include it in older vulnerabilities too
                        if (
                            existing_entry.type == dso.model.Datatype.VULNERABILITY and
                            (summary := metadata_entry.data.get('summary'))
                        ):
                            if 'summary' in existing_entry.data:
                                del existing_entry.data['summary']
                            existing_entry.data = dict(
                                **existing_entry.data,
                                summary=summary,
                            )

                        del existing_entry.meta['last_update']
                        existing_entry.meta = dict(
                            **existing_entry.meta,
                            last_update=metadata_entry.meta['last_update'],
                        )
                        break

                updated_artefacts.append(metadata_entry)

                if entry_already_exists:
                    continue

                if reusable_discovery_date:
                    metadata_entry.discovery_date = reusable_discovery_date

                session.add(metadata_entry)

            # remove metadata for seen ocm resources which was not part of supplied data
            for _, existing_entries in existing_artefacts.items():
                for old_metadata in existing_entries:
                    resource_key = {
                        'component_name': old_metadata.component_name,
                        'artefact_name': old_metadata.artefact_name,
                        'type': old_metadata.type,
                        'component_version': old_metadata.component_version,
                        'artefact_version': old_metadata.artefact_version,
                        'artefact_kind': old_metadata.artefact_kind,
                        'artefact_type': old_metadata.artefact_type,
                        # see TODO above
                        # 'artefact_extra_id': old_metadata.artefact_extra_id_normalised,
                    }
                    if old_metadata.type == dso.model.Datatype.COMPLIANCE_SNAPSHOTS:
                        resource_key['cfg_name'] = old_metadata.data.get('cfg_name')
                    key = frozenset([(k, v) for k, v in resource_key.items()])

                    if key in seen_ocm_resources_for_type:
                        session.query(dm.ArtefactMetaData).filter(
                            du.ArtefactMetadataFilters.by_single_scan_result(old_metadata)
                        ).delete()

            session.commit()
        except:
            session.rollback()
            raise

        resp.status = falcon.HTTP_CREATED # pylint: disable=E1101

    def on_delete(self, req: falcon.Request, resp: falcon.Response):
        '''
        delete artefact-metadata from delivery-db

        **expected body:**

            - entries: <array> \n
                - artefact: <object> \n
                    - component_name: <str> \n
                    - component_version: <str> \n
                    - artefact: <object> \n
                        - artefact_name: <str> \n
                        - artefact_version: <str> \n
                        - artefact_type: <str> \n
                        - artefact_extra_id: <object> \n
                - meta: <object> \n
                    - type: <str> \n
                    - datasource: <str> \n
                - data: <object> # schema depends on meta.type \n
                - discovery_date: <str of format YYYY-MM-DD> \n
        '''
        body = req.media
        entries: list[dict] = body.get('entries')

        session: ss.Session = req.context.db_session

        type_hooks = {
            datetime.date:
            lambda date: datetime.datetime.strptime(date, '%Y-%m-%d').date() if date else None,
        }

        try:
            for entry in entries:
                entry = _fill_default_values(entry)

                artefact_metadata = du.to_db_artefact_metadata(
                    artefact_metadata=dacite.from_dict(
                        data_class=dso.model.ArtefactMetadata,
                        data=entry,
                        config=dacite.Config(type_hooks=type_hooks),
                    ),
                )

                session.query(dm.ArtefactMetaData).filter(
                    du.ArtefactMetadataFilters.by_single_scan_result(artefact_metadata)
                ).delete()

                session.commit()
        except:
            session.rollback()
            raise

        resp.status = falcon.HTTP_NO_CONTENT # pylint: disable=E1101


def reuse_discovery_date_if_possible(
    old_metadata: dm.ArtefactMetaData,
    new_metadata: dm.ArtefactMetaData,
) -> datetime.date | None:
    new_id = new_metadata.data.get('id', dict())
    old_id = old_metadata.data.get('id', dict())

    if new_id.get('source') != old_id.get('source'):
        return

    if (
        new_id.get('source') == dso.model.Datasource.BDBA and
        new_id.get('package_name') == old_id.get('package_name') and
        new_metadata.type == dso.model.Datatype.VULNERABILITY and
        new_metadata.data.get('cve') == old_metadata.data.get('cve')
    ):
        # found the same cve in existing entry, independent of the component-/
        # resource-/package-version, so we must re-use its discovery date
        return old_metadata.discovery_date

    if (
        new_id.get('source') == dso.model.Datasource.BDBA and
        new_id.get('package_name') == old_id.get('package_name') and
        new_metadata.type == dso.model.Datatype.LICENSE and
        new_metadata.data.get('license').get('name') == old_metadata.data.get('license').get('name')
    ):
        # found the same license in existing entry, independent of the component-/
        # resource-/package-version, so we must re-use its discovery date
        return old_metadata.discovery_date


def check_if_findigs_are_equal(
    old_metadata: dm.ArtefactMetaData,
    new_metadata: dm.ArtefactMetaData,
) -> bool:
    if (
        new_metadata.type not in (
        dso.model.Datatype.STRUCTURE_INFO,
        dso.model.Datatype.VULNERABILITY,
        dso.model.Datatype.LICENSE,
        dso.model.Datatype.COMPLIANCE_SNAPSHOTS,
    )):
        # because of pre-filtering, findings have to be equal
        return True

    if (
        new_metadata.type == dso.model.Datatype.STRUCTURE_INFO and
        du.normalise_object(new_metadata.data.get('id'))
            == du.normalise_object(old_metadata.data.get('id')) and
        du.normalise_object(new_metadata.data.get('scan_id'))
            == du.normalise_object(old_metadata.data.get('scan_id'))
    ):
        return True

    if (
        new_metadata.type == dso.model.Datatype.VULNERABILITY and
        du.normalise_object(new_metadata.data.get('id'))
            == du.normalise_object(old_metadata.data.get('id')) and
        du.normalise_object(new_metadata.data.get('scan_id'))
            == du.normalise_object(old_metadata.data.get('scan_id')) and
        new_metadata.data.get('cve') == old_metadata.data.get('cve')
    ):
        return True

    if (
        new_metadata.type == dso.model.Datatype.LICENSE and
        du.normalise_object(new_metadata.data.get('id'))
            == du.normalise_object(old_metadata.data.get('id')) and
        du.normalise_object(new_metadata.data.get('scan_id'))
            == du.normalise_object(old_metadata.data.get('scan_id')) and
        new_metadata.data.get('license').get('name')
            == old_metadata.data.get('license').get('name')
    ):
        return True

    if (
        new_metadata.type == dso.model.Datatype.COMPLIANCE_SNAPSHOTS and
        new_metadata.data.get('cfg_name') == old_metadata.data.get('cfg_name') and
        new_metadata.data.get('correlation_id') == old_metadata.data.get('correlation_id')
    ):
        return True

    return False


def _fill_default_values(
    raw: dict,
) -> dict:
    meta = raw['meta']
    if not meta.get('last_update'):
        meta['last_update'] = datetime.datetime.now().isoformat()

    return raw
