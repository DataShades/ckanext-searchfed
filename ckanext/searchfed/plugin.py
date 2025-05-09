import logging
import requests
import re
import copy
import six
from flask import has_request_context

import ckan.plugins as plugins
import ckan.plugins.toolkit as toolkit
import ckan.lib.helpers as h

from ckan.lib.base import abort
from ckan.common import request, c
from ckan.plugins.toolkit import config
from ckanext.toolbelt.decorators import Cache


log = logging.getLogger(__name__)


class SearchfedPlugin(plugins.SingletonPlugin):
    plugins.implements(plugins.IConfigurer)
    plugins.implements(plugins.IPackageController, inherit=True)

    search_fed_dict = dict(
        list(zip(*[iter(toolkit.aslist(config.get("ckan.search_federation", [])))] * 2))
    )
    search_fed_this_label = config.get("ckan.search_federation.label", "")
    search_fed_keys = toolkit.aslist(
        config.get("ckan.search_federation.extra_keys", "harvest_portal")
    )
    search_fed_labels = list(search_fed_dict.keys()) + [search_fed_this_label]
    use_remote_facets = toolkit.asbool(
        config.get("ckan.search_federation.use_remote_facet_results", False)
    )
    search_fed_label_blacklist = toolkit.aslist(
        config.get(
            "ckan.search_federation.label_blacklist",
            "owner_org harvest_source_id user_id",
        )
    )
    search_fed_dataset_whitelist = toolkit.aslist(
        config.get("ckan.search_federation.dataset_whitelist", "dataset")
    )

    # IConfigurer

    def update_config(self, config_):
        toolkit.add_template_directory(config_, "templates")
        toolkit.add_public_directory(config_, "public")
        toolkit.add_resource("fanstatic", "ckanext-searchfed")

    # IPackageController

    def before_dataset_search(self, search_params):
        limit = int(config.get("ckan.search_federation.min_search_results", 20))
        rows = search_params.get("rows", None)
        search_params["rows"] = rows if rows is not None else limit
        return search_params

    def after_dataset_search(self, search_results, search_params):
        # Skip remote dataset search if running outside a web request context
        #  (e.g., in a CLI command)
        if not has_request_context():
            return search_results

        limit = int(config.get("ckan.search_federation.min_search_results", 20))

        def _append_remote_search(
            search_keys, remote_org_label, remote_org_url, fed_labels, type_whitelist
        ):
            local_results_num = len(search_results["results"])
            # query.run increase by 1, so we need to reduce by 1
            limit = search_params.get("rows") - 1
            current_page = request.args.get("page", 1)
            try:
                current_page = int(current_page)
                if current_page < 1:
                    raise ValueError("Negative number not allowed")
            except ValueError:
                abort(400, ('"page" parameter must be a positive integer'))

            fq = " ".join(
                f"-{key}:{val}"
                for key in search_keys
                for val in fed_labels
                if key and val
            )
            fq += " " + search_params["fq"][0]

            count_only = False
            start = search_params.get("start", 0)

            datasets_per_page = int(config.get("ckan.datasets_per_page", 20))
            remote_start = 0

            if local_results_num > 0:
                remote_limit = datasets_per_page - local_results_num
                if remote_limit <= 0:
                    count_only = True
            else:
                remote_limit = datasets_per_page
                if current_page > 1:
                    remote_start = (
                        current_page * datasets_per_page
                        - toolkit.g.local_item_count
                        - datasets_per_page
                    )

            q = search_params["q"]
            for key, value in list(search_params["extras"].items()):
                if not key:
                    continue
                q += "&" + key + "=" + value

            params = {
                "q": q,
                "fq": fq,
                "facet.field": '["organization", "license_id",\
                    "tags", "group", "res_format"]',
                "rows": remote_limit,
                "start": remote_start,
                "sort": search_params["sort"],
            }

            # passing params as a unique key for cache
            @Cache(3600)
            def _fetch_data(fetch_start, fetch_num):
                url = remote_org_url + "/api/3/action/package_search"
                try:
                    # import pdb; pdb.set_trace()
                    resp = requests.get(url, params=params)
                    log.info(f"API endpoint: {resp.url}")
                except Exception as err:
                    log.warn(f"Unable to connect to {url}: {err}")
                    return
                if not resp.ok:
                    log.warn(
                        f"[fetch data] {remote_org_url}: {resp.status_code} {resp.reason}"
                    )
                    return

                return resp.json()

            remote_results = _fetch_data(remote_start, remote_limit)

            # Only continue if the remote fetch was successful
            if not remote_results:
                return search_results

            result = remote_results["result"]
            search_results["count"] += result["count"]

            if not count_only:
                remote_results_num = len(result["results"])
                if remote_results_num <= remote_limit + remote_start:
                    if result["count"] > remote_results_num:
                        # While the result count reports all remote matches, the number of results may be limited
                        # by the CKAN install. Here our query has extended beyond the actual returned results, so
                        # we re-issue a more refined query starting and ending at precisely where we want (since
                        # we have already acquired the total count)
                        temp_results = _fetch_data(
                            remote_start,
                            min(result["count"] - remote_start, remote_limit),
                        )
                        if temp_results:
                            result["results"] = temp_results["result"]["results"]

                for dataset in result["results"]:
                    extras = dataset.get("extras", [])
                    if not h.get_pkg_dict_extra(dataset, "harvest_url"):
                        extras += [
                            {
                                "key": "harvest_url",
                                "value": remote_org_url + "/dataset/" + dataset["id"],
                            }
                        ]
                    for k in search_keys:
                        if not h.get_pkg_dict_extra(dataset, k):
                            extras += [{"key": k, "value": remote_org_label}]
                    if not h.get_pkg_dict_extra(dataset, "federation_source"):
                        extras += [
                            {"key": "federation_source", "value": remote_org_url}
                        ]
                    dataset.update(extras=extras, harvest_source_title=remote_org_label)
                if not limit or start > search_results["count"]:
                    search_results["results"] = []
                elif toolkit.g.local_item_count < limit + start:
                    search_results["results"] += result["results"]
                if "search_facets" in result and self.use_remote_facets:
                    search_results["search_facets"] = _merge_facets(
                        search_results["search_facets"], result["search_facets"]
                    )

        # If the search has failed to produce a full page of results, we augment
        toolkit.g.local_item_count = search_results["count"]
        with_remote = toolkit.asbool(
            config.get("ckan.search_federation.api_federation", False)
        )

        bp = toolkit.get_endpoint()[0]
        if not with_remote or (bp and bp != "dataset"):
            return search_results

        if search_results["count"] < limit and not re.search(
            "|".join(self.search_fed_label_blacklist), search_params["fq"][0]
        ):
            for key, val in six.iteritems(self.search_fed_dict):
                _append_remote_search(
                    self.search_fed_keys,
                    key,
                    val,
                    self.search_fed_labels,
                    self.search_fed_dataset_whitelist,
                )

        return search_results


def _merge_facets(first, second):
    result = copy.deepcopy(first)
    data = {
        k: {f["name"]: f for f in group["items"]} for k, group in list(second.items())
    }
    for key, new_facets in list(data.items()):
        old_group = result.setdefault(key, {"items": [], "title": key})
        for f in old_group["items"]:
            new_facet = new_facets.pop(f["name"], None)
            if new_facet:
                f["count"] += new_facet["count"]
        old_group["items"].extend(list(new_facets.values()))
    return result
