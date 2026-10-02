"""Roteirização por estrada (OSRM sobre OpenStreetMap), com cache em disco.

Usado para dar aos troncos o traçado real da rodovia, o km real e o nome da via (BR-116, BR-277...),
e para medir o km real das pontas (cidade -> polo do tronco).
Servidor padrão: demo público do OSRM (1 requisição por vez, com pausa). Troque por OSRM próprio em MALHA_OSRM.
"""
import collections
import hashlib
import json
import math
import os
import time
import urllib.request

from .api import DATA

OSRM = os.environ.get("MALHA_OSRM", "https://router.project-osrm.org")
CACHE = os.path.join(DATA, "osrm")
PAUSA = float(os.environ.get("MALHA_OSRM_PAUSA", "0.35"))


def km(a, b):
    la1, lo1, la2, lo2 = map(math.radians, (a[0], a[1], b[0], b[1]))
    h = math.sin((la2 - la1) / 2) ** 2 + math.cos(la1) * math.cos(la2) * math.sin((lo2 - lo1) / 2) ** 2
    return 2 * 6371 * math.asin(math.sqrt(h))


def _get(url):
    os.makedirs(CACHE, exist_ok=True)
    fn = os.path.join(CACHE, hashlib.sha1(url.encode()).hexdigest() + ".json")
    if os.path.exists(fn):
        return json.load(open(fn))
    for t in range(5):
        try:
            d = json.load(urllib.request.urlopen(urllib.request.Request(url, headers={"User-Agent": "malha-cordenonsi"}), timeout=60))
            break
        except urllib.error.HTTPError as e:
            if e.code == 400:
                d = {"code": "InvalidQuery"}
                break
            time.sleep(2 ** (t + 1))
        except Exception:
            time.sleep(2 ** (t + 1))
    else:
        return {"code": "Falha"}
    time.sleep(PAUSA)
    json.dump(d, open(fn, "w"))
    return d


def _coords(pts):
    return ";".join(f"{lo:.5f},{la:.5f}" for la, lo in pts)


def rota(pontos):
    """Rota por estrada passando pelos pontos (lat, lng). Devolve dict(coords, km, legs, refs) ou None."""
    d = _get(f"{OSRM}/route/v1/driving/{_coords(pontos)}?overview=full&geometries=geojson&steps=true")
    if d.get("code") != "Ok":
        return None
    r = d["routes"][0]
    refs = collections.Counter()
    for leg in r["legs"]:
        for s in leg["steps"]:
            for ref in (s.get("ref") or "").split(";"):
                ref = ref.strip().replace(" ", "")
                if ref:
                    refs[ref] += s["distance"] / 1000
    return dict(coords=[(la, lo) for lo, la in r["geometry"]["coordinates"]], km=r["distance"] / 1000,
                legs=[l["distance"] / 1000 for l in r["legs"]], refs=refs)


def rota_limpa(pontos, fixos=(0, -1), max_fator=1.45):
    """Roteia pelos pontos; se algum trecho entre pontos der volta (km por estrada >> linha reta,
    típico de ponto colado na pista contrária), remove o ponto intermediário culpado e tenta de novo."""
    pts = list(pontos)
    for _ in range(6):
        r = rota(pts)
        if r is None:
            if len(pts) <= 2:
                return None
            pts = pts[:: 2] + ([pts[-1]] if len(pts) % 2 == 0 else [])
            continue
        ruins = [i for i, (a, b, lk) in enumerate(zip(pts, pts[1:], r["legs"])) if lk > max_fator * km(a, b) + 8]
        if not ruins:
            return r
        tirar = set()
        for i in ruins:  # tira o ponto intermediário do trecho ruim (nunca as pontas)
            if 0 < i + 1 < len(pts) - 1:
                tirar.add(i + 1)
            elif 0 < i < len(pts) - 1:
                tirar.add(i)
        if not tirar:
            return r
        pts = [p for k, p in enumerate(pts) if k not in tirar]
    return r


def matriz(fontes, destinos):
    """km por estrada de várias fontes até 1 destino, ou de 1 fonte até vários destinos (lotes de 90)."""
    um_destino = len(destinos) == 1
    muitos = fontes if um_destino else destinos
    unico = destinos[0] if um_destino else fontes[0]
    out = []
    for i in range(0, len(muitos), 90):
        lote = muitos[i:i + 90]
        pts = lote + [unico]
        idx = ";".join(str(k) for k in range(len(lote)))
        q = f"sources={idx}&destinations={len(lote)}" if um_destino else f"sources={len(lote)}&destinations={idx}"
        d = _get(f"{OSRM}/table/v1/driving/{_coords(pts)}?{q}&annotations=distance")
        if d.get("code") != "Ok":
            out += [None] * len(lote)
            continue
        vals = [row[0] for row in d["distances"]] if um_destino else d["distances"][0]
        out += [(x / 1000 if x is not None else None) for x in vals]
    return out
