"""Gera o dashboard HTML (arquivo único) a partir de data/saida/malha.json.gz.

Uso: python -m malha.dashboard   ->  data/saida/dashboard_malha.html
"""
import json
import os

from .api import ler
from .processa import SAIDA


def main():
    dados = ler(os.path.join(SAIDA, "malha.json.gz"))
    tpl = open(os.path.join(os.path.dirname(os.path.abspath(__file__)), "dashboard.html"), encoding="utf-8").read()
    js = json.dumps(dados, ensure_ascii=False, separators=(",", ":")).replace("</", "<\\/")
    ref = os.path.join(os.path.dirname(os.path.abspath(__file__)), "ref")
    html = tpl.replace("/*__LEAFLET_CSS__*/", open(os.path.join(ref, "leaflet.css"), encoding="utf-8").read())
    html = html.replace("/*__LEAFLET_JS__*/", open(os.path.join(ref, "leaflet.js"), encoding="utf-8").read())
    html = html.replace("/*__DADOS__*/null", js)
    destino = os.path.join(SAIDA, "dashboard_malha.html")
    open(destino, "w", encoding="utf-8").write(html)
    print(destino, f"{os.path.getsize(destino) / 1e6:.1f} MB")


if __name__ == "__main__":
    main()
