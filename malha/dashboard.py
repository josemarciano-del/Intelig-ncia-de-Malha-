"""Gera o dashboard HTML (arquivo único) a partir de data/saida/malha.json.gz.

Uso: python -m malha.dashboard   ->  data/saida/dashboard_malha.html
"""
import base64
import gzip
import json
import os

from .api import ler
from .processa import SAIDA


def cidades(dados):
    """Municípios do IBGE [nome, UF, lat, lng] + cidades do exterior que aparecem nas rotas (UF 'EX')."""
    import csv
    ref = os.path.join(os.path.dirname(os.path.abspath(__file__)), "ref")
    uf = {r["codigo_uf"]: r["uf"] for r in csv.DictReader(open(os.path.join(ref, "estados.csv"), encoding="utf-8-sig"))}
    out = [[r["nome"], uf[r["codigo_uf"]], round(float(r["latitude"]), 4), round(float(r["longitude"]), 4)]
           for r in csv.DictReader(open(os.path.join(ref, "municipios.csv"), encoding="utf-8"))]
    ex = {}
    for r in dados["rotas"]:
        for nome, ll in ((r["origem"], r.get("o_ll")), (r["destino"], r.get("d_ll"))):
            if nome.endswith("/EX") and ll and nome not in ex:
                ex[nome] = [nome[:-3].title(), "EX", ll[0], ll[1]]
    return out + sorted(ex.values())


def gz64(texto):
    return base64.b64encode(gzip.compress(texto.encode("utf-8"), 9, mtime=0)).decode("ascii")


def main():
    dados = ler(os.path.join(SAIDA, "malha.json.gz"))
    tpl = open(os.path.join(os.path.dirname(os.path.abspath(__file__)), "dashboard.html"), encoding="utf-8").read()
    ref = os.path.join(os.path.dirname(os.path.abspath(__file__)), "ref")
    rod = os.path.join(ref, "rodovias.json")
    z = dict(
        dados=gz64(json.dumps(dados, ensure_ascii=False, separators=(",", ":"))),
        base=gz64(open(os.path.join(ref, "basemap.json"), encoding="utf-8").read()),
        rod=gz64(open(rod, encoding="utf-8").read() if os.path.exists(rod) else "null"),
        cid=gz64(json.dumps(cidades(dados), ensure_ascii=False, separators=(",", ":"))),
    )
    html = tpl.replace("/*__LEAFLET_CSS__*/", open(os.path.join(ref, "leaflet.css"), encoding="utf-8").read())
    html = html.replace("/*__LEAFLET_JS__*/", open(os.path.join(ref, "leaflet.js"), encoding="utf-8").read())
    html = html.replace("/*__Z__*/{}", json.dumps(z))
    destino = os.path.join(SAIDA, "dashboard_malha.html")
    open(destino, "w", encoding="utf-8").write(html)
    print(destino, f"{os.path.getsize(destino) / 1e6:.1f} MB")
    # versão para publicar como Artifact no claude.ai: sem doctype/html/head/body (a plataforma envolve)
    cab = html.split("<head>", 1)[1].split("</head>", 1)[0]
    cab = "\n".join(l for l in cab.split("\n") if "<meta" not in l)
    corpo = html.split("<body>", 1)[1].rsplit("</body>", 1)[0]
    art = os.path.join(SAIDA, "artifact_malha.html")
    open(art, "w", encoding="utf-8").write(cab.strip() + "\n" + corpo.strip() + "\n")
    print(art)


if __name__ == "__main__":
    main()
