"""Coleta de dados das APIs (retomável: o que já foi baixado não é baixado de novo).

Uso:
  python -m malha.coleta cargas 2022-01 2026-10    # cadastro de viagens por mês de criação
  python -m malha.coleta gps 2026-04 2026-09       # GPS histórico por placa x mês
  python -m malha.coleta rotas 2026-04 2026-09     # polilinha planejada de cada rota usada no período
  python -m malha.coleta autotrac 2026-04 2026-09  # placas sem GPS: estão na Autotrac ou usam outro rastreador?
"""
import glob
import os
import sys
from concurrent.futures import ThreadPoolExecutor
from datetime import date, timedelta

from .api import DATA, get, ler, salvar

WORKERS = int(os.environ.get("MALHA_WORKERS", "4"))


def meses(ini, fim):
    y, m = map(int, ini.split("-"))
    yf, mf = map(int, fim.split("-"))
    while (y, m) <= (yf, mf):
        yield y, m
        y, m = (y + 1, 1) if m == 12 else (y, m + 1)


def limites(y, m):
    a = date(y, m, 1)
    b = date(y + (m == 12), m % 12 + 1, 1) - timedelta(days=1)
    return a, b


# ---------------------------------------------------------------- cargas
def cargas_mes(ym):
    y, m = ym
    a, b = limites(y, m)
    off, n = 0, 0
    while True:
        fn = os.path.join(DATA, "cargas_pages", f"{y}-{m:02d}_{off:07d}.json.gz")
        if os.path.exists(fn) and (y, m) < (date.today().year, date.today().month):
            d = ler(fn)
        else:
            d = get("viagem/listar_cargas.php", limite=150, offset=off,
                    data_criacao_inicio=a, data_criacao_fim=b)
            salvar(d, fn)
        n += len(d["dados"])
        if not d["meta"]["tem_mais"]:
            break
        off = d["meta"]["proximo_offset"]
    print(f"cargas {y}-{m:02d}: {n}", flush=True)
    return n


def iter_cargas(ini=None, fim=None):
    """Itera as cargas baixadas (sem duplicar), opcionalmente filtrando por mês de criação 'AAAA-MM'."""
    vistos = set()
    for fn in sorted(glob.glob(os.path.join(DATA, "cargas_pages", "*.json.gz"))):
        ym = os.path.basename(fn)[:7]
        if (ini and ym < ini) or (fim and ym > fim):
            continue
        for x in ler(fn)["dados"]:
            if x["car_codigo"] not in vistos:
                vistos.add(x["car_codigo"])
                yield x


# ---------------------------------------------------------------- GPS
def gps_placa_mes(args):
    placa, y, m = args
    fn = os.path.join(DATA, "gps", f"{y}-{m:02d}", f"{placa}.json.gz")
    if os.path.exists(fn):
        return 0
    a, b = limites(y, m)
    pts, off = [], 0
    while True:
        d = get("autotrac/historico_posicoes.php", placa=placa, data_inicio=a, data_fim=b,
                formato="lista", limite=5000, offset=off)
        if not d.get("sucesso"):
            break
        for p in d["dados"]:
            pts.append([p["data_posicao"], round(p["latitude"], 5), round(p["longitude"], 5),
                        1 if p.get("ignicao") == "Ligada" else 0, p.get("referencia") or ""])
        if not d["meta"].get("tem_mais"):
            break
        off = d["meta"]["proximo_offset"]
    salvar(pts, fn)
    return len(pts)


# ---------------------------------------------------------------- rotas planejadas
def rota_planejada(args):
    """Polilinha planejada da rota; tenta até 3 viagens (o comparativo dá 404 em algumas cargas)."""
    cod, cars = args
    fn = os.path.join(DATA, "rotas", f"{cod}.json.gz")
    if os.path.exists(fn) and not ler(fn).get("erro"):
        return
    for car in cars[:3]:
        d = get("viagem/comparativo.php", car_codigo=car)
        if d.get("sucesso"):
            break
    else:
        salvar({"codigo_rota": cod, "erro": d.get("erro")}, fn)
        return
    p = d["dados"]["camada_planejada"]
    salvar({"codigo_rota": cod, "car_codigo": car, "descricao": p.get("descricao_rota"),
            "polilinha": p.get("polilinha_encoded"),
            "km": d["dados"]["metricas_comparativo"]["distancia"]["km_planejado"]}, fn)


# ---------------------------------------------------------------- placas na Autotrac (tempo real)
def placa_autotrac(placa):
    hoje = date.today()
    d = get("autotrac/historico_posicoes.php", placa=placa, data_inicio=hoje - timedelta(days=2), data_fim=hoje,
            formato="lista", limite=1)
    x = (d.get("dados") or [{}])[0]
    return placa, bool(d.get("meta", {}).get("total_disponivel")), (x.get("veiculo") or "").split(" - ", 1)[-1].strip()


def main():
    cmd, ini, fim = sys.argv[1], sys.argv[2], sys.argv[3]
    ms = list(meses(ini, fim))
    with ThreadPoolExecutor(WORKERS) as ex:
        if cmd == "cargas":
            print("TOTAL", sum(ex.map(cargas_mes, reversed(ms))))
        elif cmd == "gps":
            # placas com viagem no período (janela de criação com 1 mês de folga antes)
            placas = {x["veiculo"]["placa"] for x in iter_cargas(f"{ms[0][0]}-{ms[0][1]:02d}", fim)
                      if x["veiculo"]["placa"]}
            tarefas = [(p, y, m) for (y, m) in ms for p in sorted(placas)]
            print(f"{len(placas)} placas x {len(ms)} meses = {len(tarefas)} consultas", flush=True)
            for i, _ in enumerate(ex.map(gps_placa_mes, tarefas), 1):
                if i % 200 == 0:
                    print(f"gps {i}/{len(tarefas)}", flush=True)
        elif cmd == "autotrac":
            # só placas sem GPS em algum mês do período: está na Autotrac (tempo real) ou usa outro rastreador?
            import json
            placas = sorted({x["veiculo"]["placa"] for x in iter_cargas(ini, fim) if x["veiculo"]["placa"]})
            falta = [p for p in placas if any(
                not os.path.exists(fn) or os.path.getsize(fn) <= 60
                for fn in (os.path.join(DATA, "gps", f"{y}-{m:02d}", f"{p}.json.gz") for y, m in ms))]
            res = {p: {"autotrac": a, "grupo": g} for p, a, g in ex.map(placa_autotrac, falta)}
            json.dump(res, open(os.path.join(DATA, "placas_autotrac.json"), "w"), ensure_ascii=False)
            print(f"{len(falta)} placas verificadas; fora da Autotrac: {sum(not v['autotrac'] for v in res.values())}")
        elif cmd == "rotas":
            por_rota = {}
            for x in iter_cargas(ini, fim):
                cod = x["rota_planejada"]["codigo_rota"]
                if cod and x["rota_planejada"].get("tem_polilinha"):
                    por_rota.setdefault(cod, []).append((x["data_criacao"], x["car_codigo"]))
            tarefas = [(c, [car for _, car in sorted(v, reverse=True)]) for c, v in por_rota.items()]
            print(f"{len(tarefas)} rotas", flush=True)
            for i, _ in enumerate(ex.map(rota_planejada, tarefas), 1):
                if i % 500 == 0:
                    print(f"rotas {i}/{len(tarefas)}", flush=True)


if __name__ == "__main__":
    main()
