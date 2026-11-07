"""Resource categories shared by request routing, prefetch and administration."""
from urllib.parse import urlsplit

POLICIES = dict(core='cache', loaders='cache', mods='disabled', java='backup',
                libraries='backup', assets='backup')
MODES = ('cache', 'redirect', 'backup', 'disabled')


def category_for(url, recorded=None):
    host, path = urlsplit(url).hostname, urlsplit(url).path
    if host in ('cdn.modrinth.com', 'edge.forgecdn.net', 'mediafilez.forgecdn.net'):
        return 'mods'
    if host == 'resources.download.minecraft.net' or recorded in ('assets', 'assets-metadata'):
        return 'assets'
    if '/products/java-runtime/' in path or recorded == 'java' or (recorded or '').startswith('java-'):
        return 'java'
    if host == 'libraries.minecraft.net' or recorded in ('libraries', 'natives', 'logging'):
        return 'libraries'
    if host in ('maven.minecraftforge.net', 'files.minecraftforge.net', 'maven.neoforged.net',
                'meta.fabricmc.net', 'maven.fabricmc.net', 'meta.quiltmc.org',
                'maven.quiltmc.org', 'authlib-injector.yushi.moe'):
        return 'loaders'
    # Unindexed runtime binaries share Mojang's object host with game downloads.
    # Known game downloads are recorded from the version metadata before use.
    if '/v1/objects/' in path and not recorded:
        filename = path.rsplit('/', 1)[-1].lower()
        if filename.endswith('.xml'):
            return 'libraries'
        if not filename.endswith(('.jar', '.json', '.txt', '.zip', '.exe')):
            return 'java'
    return 'core'


def inventory_selection(report, policies):
    """Estimate from the last deduplicated inventory; never claim loader coverage."""
    result = dict(cache_bytes=0, cache_files=0, backup_bytes=0, backup_files=0,
                  redirect_bytes=0, disabled_bytes=0)
    for category, values in report.get('categories', {}).items():
        mode = policies[category_for('', category)]
        result[mode + '_bytes'] += values['bytes']
        if mode in ('cache', 'backup'):
            result[mode + '_files'] += values['files']
    result['local_bytes'] = result['cache_bytes'] + result['backup_bytes']
    return result
