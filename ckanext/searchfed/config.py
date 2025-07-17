import ckan.plugins.toolkit as tk

SEARCH_FEDERATION = "ckan.search_federation"
SEARCH_FEDERATION_LABEL = "ckan.search_federation.label"
EXTRA_KEYS = "ckan.search_federation.extra_keys"
USE_REMOTE_FACET_RESULTS = "ckan.search_federation.use_remote_facet_results"
LABEL_BLACKLIST = "ckan.search_federation.label_blacklist"
MIN_SEARCH_RESULTS = "ckan.search_federation.min_search_results"
API_FEDERATION = "ckan.search_federation.api_federation"
SOURCE_FACET_FIELD = "ckan.search_federation.source_facet_field"
SOURCE_EXTRAS_KEY = "ckan.search_federation.source_extras_key"


def search_federation() -> list[str]:
    return tk.aslist(tk.config.get(SEARCH_FEDERATION, []))


def search_federation_label() -> str:
    return tk.config.get(SEARCH_FEDERATION_LABEL, "")


def search_fed_keys() -> list[str]:
    return tk.aslist(tk.config.get(EXTRA_KEYS, "harvest_portal"))


def use_remote_facets() -> bool:
    return tk.asbool(tk.config.get(USE_REMOTE_FACET_RESULTS, False))


def search_fed_label_blacklist() -> list[str]:
    return tk.aslist(
        tk.config.get(
            LABEL_BLACKLIST,
            "owner_org harvest_source_id user_id",
        )
    )


def min_search_results() -> int:
    return int(tk.config.get(MIN_SEARCH_RESULTS, 20))


def api_federation() -> bool:
    return tk.asbool(tk.config.get(API_FEDERATION, False))


def source_facet_field() -> str:
    return tk.config.get(SOURCE_FACET_FIELD, "")


def source_extras_key() -> str:
    return tk.config.get(SOURCE_EXTRAS_KEY, "")
