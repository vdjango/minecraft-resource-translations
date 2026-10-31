"""Collect exact Maven Central file mappings verified against Mojang SHA-1.

Only small checksum sidecars are requested. No Maven tree crawling or binaries.
Output is a packaged seed adopted automatically before incremental discovery.
"""
import argparse
import asyncio
import json
import re
import time
from pathlib import Path

from aiohttp import ClientSession, ClientTimeout, TCPConnector


async def collect(root, output):
    inventory = json.loads((root/'inventory.json').read_text(encoding='utf-8'))
    files = {i['path']: i for i in inventory['files'] if i['category'] in ('libraries', 'natives')
             and i['url'].startswith('https://libraries.minecraft.net/') and i.get('sha1')}
    mappings, failures = [], []
    sem = asyncio.Semaphore(4)
    async with ClientSession(timeout=ClientTimeout(total=12), connector=TCPConnector(limit=4),
                             headers={'User-Agent': 'Buggcraft-Mirror-Source-Discovery/0.1'}) as http:
        async def check(item):
            async with sem:
                target = 'https://repo.maven.apache.org/maven2/'+item['url'].split('minecraft.net/',1)[1]
                try:
                    async with http.get(target+'.sha1', allow_redirects=False) as response:
                        raw = await response.content.read(1024)
                        match = re.match(rb'([a-fA-F0-9]{40})(?:\s|$)', raw)
                        if response.status != 200 or not match or match[1].decode().lower() != item['sha1']:
                            raise ValueError(f'no matching checksum ({response.status})')
                    mappings.append(dict(prefix=item['path'], target=target, mode='redirect', enabled=True))
                except Exception as exc:
                    failures.append(dict(path=item['path'], error=str(exc)[:100]))
        await asyncio.gather(*(check(i) for i in files.values()))
    result = dict(generated_at=time.time(), method='Exact Maven Central .sha1 matches Mojang inventory SHA-1; clients still verify downloads',
                  checked=len(files), matched=len(mappings), sources=sorted(mappings,key=lambda r:r['prefix']),
                  checksums={row['prefix']:files[row['prefix']]['sha1'] for row in mappings},failures=failures)
    output.write_text(json.dumps(result, ensure_ascii=False, indent=2)+'\n',encoding='utf-8')
    print(json.dumps({k:result[k] for k in ('checked','matched','generated_at')}))


if __name__ == '__main__':
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--root',type=Path,default=Path('G:/BuggcraftMirror'))
    parser.add_argument('--output',type=Path,default=Path('src/mc_translate/web/mirror-sources.json'))
    args=parser.parse_args()
    asyncio.run(collect(args.root,args.output))
