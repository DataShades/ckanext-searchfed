import ckan.plugins as plugins
import ckan.plugins.toolkit as toolkit
import pylons.config as config
import ckan.lib.helpers as h
import urllib
import urllib2
import json
import re
from pylons.decorators.cache import beaker_cache

import logging

log = logging.getLogger(__name__)


class SearchfedPlugin(plugins.SingletonPlugin):
    plugins.implements(plugins.IConfigurer)
    plugins.implements(plugins.IPackageController, inherit=True)

    search_fed_dict = dict(zip(*[iter(toolkit.aslist(
        config.get('ckan.search_federation', [])))] * 2))
    search_fed_this_label = config.get('ckan.search_federation.label', '')
    search_fed_keys = toolkit.aslist(
        config.get('ckan.search_federation.extra_keys', 'harvest_portal'))
    search_fed_labels = search_fed_dict.keys() + [search_fed_this_label]
    use_remote_facets = toolkit.asbool(config.get(
        'ckan.search_federation.use_remote_facet_results', False))
    search_fed_label_blacklist = toolkit.aslist(config.get(
        'ckan.search_federation.label_blacklist',
        'owner_org harvest_source_id user_id'))
    search_fed_dataset_whitelist = toolkit.aslist(config.get(
        'ckan.search_federation.dataset_whitelist', 'dataset'))

    # IConfigurer

    def update_config(self, config_):
        toolkit.add_template_directory(config_, 'templates')
        toolkit.add_public_directory(config_, 'public')
        toolkit.add_resource('fanstatic', 'searchfed')

    # IPackageController

    def before_search(self, search_params):
        limit = int(config.get(
            'ckan.search_federation.min_search_results', 20))
        rows = search_params.get('rows', None)
        search_params['rows'] = rows if rows is not None else limit
        return search_params

    def after_search(self, search_results, search_params):
        limit = int(config.get(
            'ckan.search_federation.min_search_results', 20))

        def _append_remote_search(search_keys, remote_org_label,
                                  remote_org_url, fed_labels, type_whitelist):

            local_results_num = len(search_results['results'])
            facet_fields = search_params.get('facet.field', [])
            remote_results_num = 0
            # query.run increase by 1, so we need to reduce by 1
            limit = search_params.get('rows') - 1
            fq = " ".join(g for g in
                          map(lambda sk: " ".join(e for e in map(
                              lambda x: "-" + sk + ":" + str(x), fed_labels)),
                              search_keys))
            for fq_entry in toolkit.aslist(search_params['fq'][0]):
                fq_entry = fq_entry.replace('/"', '"').replace("//", "")
                fq_split = fq_entry.split(':', 1)
                if len(fq_split) == 2:
                    fq_key = fq_split[0]
                    fq_value = fq_split[1]
                    fq_monop = ""
                    if fq_key[0] in ['+', '-']:
                        fq_monop = fq_entry[:1]
                        fq_key = fq_key[1:]

                    # Dataset whitelist check
                    if (fq_key == 'dataset_type' and
                            fq_monop != "-" and
                            fq_value not in type_whitelist):
                        return

                    if fq_key.lower() in facet_fields:
                        fq += " " + fq_monop + fq_key + ":" + fq_value
                else:
                    fq += fq_entry

            count_only = False
            start = search_params.get('start', 0)

            if local_results_num >= start:
                remote_limit = limit - local_results_num + start
                if remote_limit <= 0:
                    count_only = True
                remote_start = 0
            else:
                remote_limit = limit
                if local_results_num:
                    remote_start = start - local_results_num
                else:
                    remote_start = 0

            @beaker_cache(expire=3600, query_args=True)
            def _fetch_data(fetch_start, fetch_num):
                data = urllib.quote(json.dumps({
                    'q': search_params['q'],
                    'fq': fq,
                    'facet.field': search_params.get('facet.field', []),
                    'rows': fetch_num,
                    'start': fetch_start,
                    'sort': search_params['sort'],
                    'extras': search_params['extras']
                }))

                try:
                    req = urllib2.Request(
                        remote_org_url + '/api/3/action/package_search', data)
                    rsp = urllib2.urlopen(req)
                except urllib2.URLError, err:
                    log.warn('Unable to connect to %r: %r' % (
                        remote_org_url + '/api/3/action/package_search', err))
                    return None
                content = rsp.read()
                return json.loads(content)

            remote_results = _fetch_data(0, 99999)

            # Only continue if the remote fetch was successful
            if remote_results is None:
                return search_results

            if count_only:
                remote_results['result']['results'] = []
            else:
                use_temp = False
                remote_results_num = len(remote_results['result']['results'])
                if remote_results_num <= remote_limit + remote_start:
                    if remote_results['result']['count'] > remote_results_num:
                        # While the result count reports all remote matches, the number of results may be limited
                        # by the CKAN install. Here our query has extended beyond the actual returned results, so
                        # we re-issue a more refined query starting and ending at precisely where we want (since
                        # we have already acquired the total count)
                        temp_results = _fetch_data(remote_start, min(
                            remote_results['result']['count'] - remote_start,
                            remote_limit))
                        if temp_results:
                            use_temp = True
                            remote_results['result']['results'] = temp_results[
                                'result']['results']

                if not use_temp:
                    remote_results['result']['results'] = remote_results[
                        'result']['results'][
                        remote_start:remote_limit + remote_start]
            for dataset in remote_results['result']['results']:
                extras = dataset.get('extras', [])
                if not h.get_pkg_dict_extra(dataset, 'harvest_url'):
                    extras += [
                        {
                            'key': 'harvest_url',
                            'value': remote_org_url + '/dataset/' + dataset[
                                'id']
                        }
                    ]
                for k in search_keys:
                    if not h.get_pkg_dict_extra(dataset, k):
                        extras += [{'key': k, 'value': remote_org_label}]
                if not h.get_pkg_dict_extra(dataset, 'federation_source'):
                    extras += [{'key': 'federation_source',
                                'value': remote_org_url}]
                dataset.update(
                    extras=extras, harvest_source_title=remote_org_label)
            search_results['count'] += remote_results['result']['count']
            if not count_only:
                if (toolkit.c.local_item_count + remote_results_num <= start) and not used_controller:
                    search_results['results'] = []
                elif (not(toolkit.c.local_item_count >= limit) or
                        (search_results['count'] == limit + start) or
                        not(limit + start < search_results['count'])):
                    search_results['results'] += remote_results['result'][
                                                                'results']
                if ('search_facets' in remote_results['result'] and
                        self.use_remote_facets):
                    search_results['search_facets'] = remote_results['result'][
                                                            'search_facets']

        # If the search has failed to produce a full page of results, we augment
        toolkit.c.local_item_count = search_results['count']
        include_remote_datasets = toolkit.asbool(
            config.get('ckan.search_federation.api_federation', False))
        route_dict = toolkit.request.environ.get('pylons.routes_dict')
        route_ctrl = route_dict['controller']
        used_controller = True if route_ctrl != 'api' else False
        if include_remote_datasets or (
                not include_remote_datasets and used_controller):
            if search_results['count'] < limit and not re.search(
                    "|".join(self.search_fed_label_blacklist),
                    search_params['fq'][0]):
                for key, val in self.search_fed_dict.iteritems():
                    _append_remote_search(
                        self.search_fed_keys, key, val, self.search_fed_labels,
                        self.search_fed_dataset_whitelist)

        return search_results
