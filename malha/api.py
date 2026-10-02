"""Acesso às APIs da Cordenonsi. Token vem da variável de ambiente CRD_TOKEN (ou arquivo .env)."""
import gzip
import json
import os
import time
import urllib.parse
import urllib.request

RAIZ = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
DATA = os.environ.get("MALHA_DATA", os.path.join(RAIZ, "data"))
BASE = "https://one.cordenonsi.com.br/api/externo/"


def token():
    t = os.environ.get("CRD_TOKEN")
    if not t and os.path.exists(os.path.join(RAIZ, ".env")):
        for linha in open(os.path.join(RAIZ, ".env")):
            if linha.startswith("CRD_TOKEN="):
                t = linha.split("=", 1)[1].strip()
    if not t:
        raise SystemExit("Defina CRD_TOKEN (variável de ambiente ou arquivo .env)")
    return t


def get(endpoint, tentativas=5, **params):
    url = BASE + endpoint + "?" + urllib.parse.urlencode(params)
    req = urllib.request.Request(url, headers={"Authorization": f"Bearer {token()}", "Accept": "application/json"})
    for t in range(tentativas):
        try:
            return json.load(urllib.request.urlopen(req, timeout=300))
        except urllib.error.HTTPError as e:
            if e.code in (400, 401, 403, 404):
                try:
                    return json.load(e)
                except Exception:
                    return {"sucesso": False, "erro": {"codigo": f"http_{e.code}"}}
            time.sleep(2 ** (t + 1))
        except Exception:
            time.sleep(2 ** (t + 1))
    raise RuntimeError(f"falha: {url}")


def salvar(obj, caminho):
    os.makedirs(os.path.dirname(caminho), exist_ok=True)
    tmp = caminho + ".tmp"
    with gzip.open(tmp, "wt", encoding="utf-8") as f:
        json.dump(obj, f, ensure_ascii=False)
    os.replace(tmp, caminho)


def ler(caminho):
    with gzip.open(caminho, "rt", encoding="utf-8") as f:
        return json.load(f)
