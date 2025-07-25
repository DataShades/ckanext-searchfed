import copy
import logging
import re

import requests
import six
from flask import has_request_context

import ckan.lib.helpers as h
import ckan.plugins as p
import ckan.plugins.toolkit as tk
from ckan.common import request
from ckan.lib.base import abort

from ckanext.toolbelt.decorators import Cache

from ckanext.searchfed import config

log = logging.getLogger(__name__)


class SearchfedPlugin(p.SingletonPlugin):
    p.implements(p.IConfigurer)
    p.implements(p.IPackageController, inherit=True)

    search_fed_dict = dict(list(zip(*[iter(config.search_federation())] * 2)))
    search_fed_this_label = config.search_federation_label()
    search_fed_keys = config.search_fed_keys()
    search_fed_labels = list(search_fed_dict.keys()) + [search_fed_this_label]
    use_remote_facets = config.use_remote_facets()
    search_fed_label_blacklist = config.search_fed_label_blacklist()

    # IConfigurer

    def update_config(self, config_):
        tk.add_template_directory(config_, "templates")
        tk.add_public_directory(config_, "public")
        tk.add_resource("fanstatic", "ckanext-searchfed")

    # IPackageController

    def before_dataset_search(self, search_params):
        limit = config.min_search_results()
        rows = search_params.get("rows", None)
        search_params["rows"] = rows if rows is not None else limit
        return search_params

    def after_dataset_search(self, search_results, search_params):
        # Skip remote dataset search if running outside a web request context
        #  (e.g., in a CLI command)
        if not has_request_context():
            return search_results

        limit = config.min_search_results()

        def _append_remote_search(
            search_keys, remote_org_label, remote_org_url, fed_labels
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

            datasets_per_page = int(tk.config.get("ckan.datasets_per_page", 20))
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
                        - tk.g.local_item_count
                        - datasets_per_page
                    )

            q = search_params["q"]

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
            def _fetch_data(remote_url, remote_search_params):
                url = remote_url + "/api/3/action/package_search"
                try:
                    # import pdb; pdb.set_trace()
                    resp = requests.get(url, params=remote_search_params)
                    log.info(f"API endpoint: {resp.url}")
                except Exception as err:
                    log.warning(f"Unable to connect to {url}: {err}")
                    return None
                if not resp.ok:
                    log.warning(
                        f"[fetch data] {remote_url}: {resp.status_code} {resp.reason}"
                    )
                    return None

                return resp.json()

            remote_results = _fetch_data(remote_org_url, params)

            # Only continue if the remote fetch was successful
            if not remote_results:
                return search_results

            result = remote_results["result"]

            facet_field = config.source_facet_field()
            extras_key = config.source_extras_key()

            # If the source portal facet field is used, add an item representing the
            # current portal
            if facet_field in search_results["facets"]:
                search_results["facets"][facet_field][remote_org_label] = result[
                    "count"
                ]
                search_results["search_facets"][facet_field]["items"].append(
                    {
                        "name": remote_org_label,
                        "display_name": remote_org_label,
                        "count": result["count"],
                    }
                )

                # If a source portal facet filter is applied, check whether the current
                # portal is selected. If not, exclude results from the current portal
                # from the search results.
                data_providers = search_params["extras"].get(extras_key)
                if data_providers and remote_org_label not in data_providers:
                    return None

            search_results["count"] += result["count"]

            if not count_only:
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
                elif tk.g.local_item_count < limit + start:
                    search_results["results"] += result["results"]

                if "search_facets" in result and self.use_remote_facets:
                    search_results["search_facets"] = _merge_facets(
                        search_results["search_facets"], result["search_facets"]
                    )

        # If the search has failed to produce a full page of results, we augment
        tk.g.local_item_count = search_results["count"]
        with_remote = config.api_federation()

        bp = tk.get_endpoint()[0]
        if not with_remote or (bp and bp != "dataset"):
            return search_results

        if (limit == -1 or search_results["count"] < limit) and not re.search(
            "|".join(self.search_fed_label_blacklist), search_params["fq"][0]
        ):
            for key, val in six.iteritems(self.search_fed_dict):
                _append_remote_search(
                    self.search_fed_keys,
                    key,
                    val,
                    self.search_fed_labels,
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
