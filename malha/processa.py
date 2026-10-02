"""Motor da malha: viagens -> trilhas em hexágonos -> troncos, nós e pontas -> rotas padrão.

Uso: python -m malha.processa 2026-04 2026-09
Saída: data/saida/malha.json.gz (consumido por malha.dashboard) e data/saida/malha.xlsx
"""
import collections
import glob
import math
import os
import re
import sys
from datetime import datetime, timedelta

import h3

from .api import DATA, ler, salvar
from .coleta import iter_cargas, meses

# ---------------------------------------------------------------- parâmetros
RES = 6                 # hexágono H3 ~36 km² (centro a centro ~6,5 km)
MIN_OD = 5              # hexágono é tronco se passam >= 5 pares cidade->cidade distintos...
MIN_VIAGENS_MES = 5     # ... e >= 5 viagens por mês (~1 por semana)
MIN_ACESSO_MES = 4      # entrada/saída do tronco vira nó se >= 4 viagens/mês entram ou saem ali
MAX_DEST_KM = 40        # trilha GPS precisa começar/terminar a até 40 km da 1ª/última etapa
SAIDA = os.path.join(DATA, "saida")


def km(a, b):
    la1, lo1, la2, lo2 = map(math.radians, (a[0], a[1], b[0], b[1]))
    h = math.sin((la2 - la1) / 2) ** 2 + math.cos(la1) * math.cos(la2) * math.sin((lo2 - lo1) / 2) ** 2
    return 2 * 6371 * math.asin(math.sqrt(h))


def decode(s):
    idx = lat = lng = 0
    pts = []
    while idx < len(s):
        for i in (0, 1):
            shift = res = 0
            while True:
                b = ord(s[idx]) - 63
                idx += 1
                res |= (b & 0x1F) << shift
                shift += 5
                if b < 0x20:
                    break
            d = ~(res >> 1) if res & 1 else res >> 1
            if i == 0:
                lat += d
            else:
                lng += d
        pts.append((lat / 1e5, lng / 1e5))
    return pts


def celulas(pts):
    """Sequência contínua de hexágonos para uma lista de (lat, lng); preenche lacunas em linha reta."""
    out = []
    for la, lo in pts:
        c = h3.latlng_to_cell(la, lo, RES)
        if not out:
            out.append(c)
        elif c != out[-1]:
            try:
                path = h3.grid_path_cells(out[-1], c)
            except Exception:
                path = [out[-1], c]
            out += path[1:] if len(path) <= 15 else [c]
    return out


RE_CID = re.compile(r"([^,]+?)/([A-Z]{2}), ")


def cidade_ref(ref):
    """'0.31 Km SSE de Posto X - CIDADE/UF, UF - País' -> 'CIDADE/UF' (usado fora do Brasil)."""
    m = RE_CID.search(ref or "")
    return f"{m.group(1).split(' - ')[-1].strip().upper()}/{m.group(2)}" if m else ""


class Municipios:
    """Base IBGE (malha/ref) para dar nome padronizado a nós e trechos: município mais próximo."""

    def __init__(self):
        import csv
        ref = os.path.join(os.path.dirname(os.path.abspath(__file__)), "ref")
        uf = {r["codigo_uf"]: r["uf"] for r in csv.DictReader(open(os.path.join(ref, "estados.csv"), encoding="utf-8-sig"))}
        self.grade = collections.defaultdict(list)
        for r in csv.DictReader(open(os.path.join(ref, "municipios.csv"), encoding="utf-8")):
            la, lo = float(r["latitude"]), float(r["longitude"])
            self.grade[(round(la), round(lo))].append((la, lo, f"{r['nome']}/{uf[r['codigo_uf']]}"))

    def perto(self, la, lo, raio=30):
        best = (raio, None)
        for i in (-1, 0, 1):
            for j in (-1, 0, 1):
                for a, b, n in self.grade.get((round(la) + i, round(lo) + j), ()):
                    d = km((la, lo), (a, b))
                    if d < best[0]:
                        best = (d, n)
        return best[1]


def dt(s):
    return datetime.fromisoformat(s) if s else None


# ---------------------------------------------------------------- 1. viagens
def carregar_viagens(ini, fim):
    vs = []
    for x in iter_cargas(ini, fim):
        e = sorted(x.get("etapas") or [], key=lambda e: e["ordem"])
        e = [k for k in e if k.get("latitude")]
        if len(e) < 2:
            continue
        o, d = e[0], e[-1]
        rota = x["rota_planejada"]
        vs.append(dict(
            car=x["car_codigo"], criacao=x["data_criacao"][:10], cliente=x["cliente_embarcador"] or "(sem cliente)",
            placa=x["veiculo"]["placa"], status=x["status"]["descricao"], finalizada=x["status"]["finalizada"],
            ini=x["viagem"]["data_inicio"], fim=x["viagem"]["data_fim"],
            rota=rota["codigo_rota"], rota_desc=rota["descricao"] or "", km_plan=rota["km_planejado"],
            vazio="VAZIO" in (rota["descricao"] or "").upper(),
            orig=f"{o['cidade'].upper()}/{o['uf']}", dest=f"{d['cidade'].upper()}/{d['uf']}",
            o_ll=(o["latitude"], o["longitude"]), d_ll=(d["latitude"], d["longitude"]),
            intl=any(k["tipo"] == "fronteira" for k in e),
        ))
    return vs


def trilhas(vs, ms):
    """Atribui a cada viagem a sequência de hexágonos: GPS realizado quando válido, senão rota planejada."""
    rotas = {}
    for fn in glob.glob(os.path.join(DATA, "rotas", "*.json.gz")):
        r = ler(fn)
        if r.get("polilinha"):
            rotas[r["codigo_rota"]] = r
    cache_plan = {}
    por_placa = collections.defaultdict(list)
    for v in vs:
        por_placa[v["placa"]].append(v)
    motivos = collections.Counter()
    for placa, lista in por_placa.items():
        pts = []
        for y, m in ms:
            fn = os.path.join(DATA, "gps", f"{y}-{m:02d}", f"{placa}.json.gz")
            if placa and os.path.exists(fn):
                pts += ler(fn)
        ts = [dt(p[0]) for p in pts]
        ordem = sorted(range(len(pts)), key=lambda i: ts[i])
        pts = [pts[i] for i in ordem]
        ts = [ts[i] for i in ordem]
        for v in lista:
            v["fonte"], v["cells"] = None, None
            a, b = dt(v["ini"]), dt(v["fim"])
            gps_ok = False
            if not pts:
                motivos["placa sem GPS histórico"] += 1
            elif not a or not b or (b - a) < timedelta(minutes=30):
                motivos["janela inválida (início=fim)"] += 1
            elif (b - a) > timedelta(days=15):
                motivos["janela > 15 dias"] += 1
            else:
                import bisect
                i, j = bisect.bisect_left(ts, a - timedelta(hours=1)), bisect.bisect_right(ts, b)
                tr = [(p[1], p[2]) for p in pts[i:j]]
                if len(tr) < 3:
                    motivos["sem GPS na janela"] += 1
                else:
                    # recorta a trilha do ponto mais perto da origem até o mais perto do destino
                    do = [km(p, v["o_ll"]) for p in tr]
                    io = min(range(len(tr)), key=do.__getitem__)
                    dd = [km(p, v["d_ll"]) for p in tr]
                    idd = max(range(io, len(tr)), key=lambda k: -dd[k])
                    if do[io] > MAX_DEST_KM or dd[idd] > MAX_DEST_KM or idd - io < 1:
                        motivos["GPS não liga origem ao destino"] += 1
                    else:
                        gps_ok = True
                        v["fonte"], v["cells"] = "gps", celulas(tr[io: idd + 1])
                    for p in pts[i:j]:
                        if p[4]:
                            v.setdefault("_refs", []).append((p[1], p[2], p[4]))
            if not gps_ok and v["rota"] in rotas:
                if v["rota"] not in cache_plan:
                    cache_plan[v["rota"]] = celulas(decode(rotas[v["rota"]]["polilinha"]))
                v["fonte"], v["cells"] = "planejada", cache_plan[v["rota"]]
            if gps_ok and v["rota"] in rotas:
                # aderência: % do realizado dentro do corredor planejado (+-1 hexágono)
                if v["rota"] not in cache_plan:
                    cache_plan[v["rota"]] = celulas(decode(rotas[v["rota"]]["polilinha"]))
                corr = set()
                for c in cache_plan[v["rota"]]:
                    corr.update(h3.grid_disk(c, 1))
                v["aderencia"] = round(100 * sum(c in corr for c in v["cells"]) / len(v["cells"]))
    return motivos, rotas


# ---------------------------------------------------------------- 2. troncos
def construir_malha(vs, n_meses):
    MIN_VIAGENS = MIN_VIAGENS_MES * n_meses
    cell_v = collections.Counter()
    cell_od = collections.defaultdict(set)
    edge_v = collections.Counter()
    nomes = collections.defaultdict(collections.Counter)
    for v in vs:
        if not v["cells"]:
            continue
        od = (v["orig"], v["dest"])
        for c in set(v["cells"]):
            cell_v[c] += 1
            cell_od[c].add(od)
        for a, b in {tuple(sorted(p)) for p in zip(v["cells"], v["cells"][1:]) if p[0] != p[1]}:
            edge_v[(a, b)] += 1
        for la, lo, ref in v.pop("_refs", []):
            cid = cidade_ref(ref)
            if cid:
                nomes[h3.latlng_to_cell(la, lo, RES)][cid] += 1
    tronco = {c for c in cell_v if cell_v[c] >= MIN_VIAGENS and len(cell_od[c]) >= MIN_OD}

    # grafo dos hexágonos-tronco; árvore geradora máxima remove zigue-zague, depois devolve
    # arestas fortes que fecham ciclos longos (anéis reais da malha)
    arestas = sorted(((w, a, b) for (a, b), w in edge_v.items()
                      if a in tronco and b in tronco and w >= MIN_VIAGENS / 2), reverse=True)
    pai = {c: c for c in tronco}

    def raiz(c):
        while pai[c] != c:
            pai[c] = pai[pai[c]]
            c = pai[c]
        return c

    adj = collections.defaultdict(set)
    sobra = []
    for w, a, b in arestas:
        ra, rb = raiz(a), raiz(b)
        if ra != rb:
            pai[ra] = rb
            adj[a].add(b)
            adj[b].add(a)
        else:
            sobra.append((w, a, b))
    for w, a, b in sobra:
        if w >= 2 * MIN_VIAGENS and distancia_grafo(adj, a, b, 12) > 12:
            adj[a].add(b)
            adj[b].add(a)

    # poda de espinhos curtos (ramos de 1-2 hexágonos pendurados em bifurcação)
    for _ in range(3):
        for c in [c for c in list(adj) if len(adj[c]) == 1]:
            cadeia, cur, prev = [c], c, None
            while len(adj[cur]) == 2 or (cur == c and len(adj[cur]) == 1):
                nxt = [n for n in adj[cur] if n != prev]
                if not nxt:
                    break
                prev, cur = cur, nxt[0]
                if len(adj[cur]) != 2:
                    break
                cadeia.append(cur)
            if len(adj.get(cur, ())) >= 3 and len(cadeia) <= 2:
                for k in cadeia:
                    for n in list(adj[k]):
                        adj[n].discard(k)
                    adj.pop(k, None)
    adj = {c: s for c, s in adj.items() if s}
    return adj, cell_v, cell_od, nomes


def distancia_grafo(adj, a, b, limite):
    vistos, fila = {a: 0}, collections.deque([a])
    while fila:
        c = fila.popleft()
        if vistos[c] >= limite:
            continue
        for n in adj[c]:
            if n not in vistos:
                vistos[n] = vistos[c] + 1
                if n == b:
                    return vistos[n]
                fila.append(n)
    return limite + 1


def segmentar(adj, nos_extra):
    """Quebra o grafo em trechos entre nós (bifurcação, ponta de linha ou ponto de acesso)."""
    nos = {c for c, s in adj.items() if len(s) != 2} | (nos_extra & set(adj))
    trechos, usados = [], set()
    for n in nos:
        for viz in adj[n]:
            if (n, viz) in usados:
                continue
            cadeia = [n, viz]
            while cadeia[-1] not in nos:
                nxt = [k for k in adj[cadeia[-1]] if k != cadeia[-2]]
                if not nxt:
                    break
                cadeia.append(nxt[0])
            usados.add((cadeia[0], cadeia[1]))
            usados.add((cadeia[-1], cadeia[-2]))
            trechos.append(cadeia)
    # anéis sem nó nenhum
    soltos = set(adj) - {c for t in trechos for c in t}
    while soltos:
        c = soltos.pop()
        nos.add(c)
        cadeia = [c, next(iter(adj[c]))]
        while cadeia[-1] != c:
            nxt = [k for k in adj[cadeia[-1]] if k != cadeia[-2]]
            cadeia.append(nxt[0])
            soltos.discard(cadeia[-1])
        trechos.append(cadeia)
    return trechos, nos


def suavizar(ll, janela=2):
    """Média móvel do traçado (centros de hexágono fazem zigue-zague); mantém as pontas fixas."""
    if len(ll) < 5:
        return ll
    out = [ll[0]]
    for i in range(1, len(ll) - 1):
        a, b = max(0, i - janela), min(len(ll), i + janela + 1)
        out.append((sum(p[0] for p in ll[a:b]) / (b - a), sum(p[1] for p in ll[a:b]) / (b - a)))
    return out + [ll[-1]]


def percorrer(cells, cell_seg):
    """Sequência de (trecho, primeiro_hex, último_hex) que a viagem percorre; ignora toques de 1 hexágono."""
    seq = []
    for c in cells:
        s = cell_seg.get(c)
        if s is None:
            continue
        if seq and seq[-1][0] == s:
            seq[-1][2] = c
            seq[-1][3] += 1
        else:
            seq.append([s, c, c, 1])
    seq = [x for x in seq if x[3] >= 2]
    out = []
    for x in seq:
        if out and out[-1][0] == x[0]:
            out[-1][2] = x[2]
        else:
            out.append(x)
    return out


def main():
    ini, fim = sys.argv[1], sys.argv[2]
    ms = list(meses(ini, fim))
    vs = carregar_viagens(ini, fim)
    print("viagens com etapas:", len(vs), flush=True)
    motivos, rotas = trilhas(vs, ms)
    adj, cell_v, cell_od, nomes = construir_malha(vs, len(ms))

    # 1ª passada: trechos entre bifurcações; depois pontos de acesso viram nós
    trechos, nos = segmentar(adj, set())
    acesso = collections.Counter()
    cell_seg = {c: i for i, t in enumerate(trechos) for c in t[1:-1]}
    for c in nos:
        cell_seg[c] = ("n", c)
    for v in vs:
        if v["cells"]:
            tc = [c for c in v["cells"] if c in adj]
            if tc:
                acesso[tc[0]] += 1
                acesso[tc[-1]] += 1
    extra = set()
    for c, n in acesso.most_common():
        if n < MIN_ACESSO_MES * len(ms):
            break
        if c in nos or any(km(h3.cell_to_latlng(c), h3.cell_to_latlng(e)) < 25 for e in extra | nos):
            continue
        extra.add(c)
    trechos, nos = segmentar(adj, extra)
    print("hexágonos tronco:", len(adj), "| trechos:", len(trechos), "| nós:", len(nos), flush=True)

    mun = Municipios()

    def nome_cell(c, raio=4):
        n = mun.perto(*h3.cell_to_latlng(c))
        if n:
            return n
        for k in range(raio + 1):
            cont = collections.Counter()
            for n in h3.grid_ring(c, k) if k else [c]:
                cont.update(nomes.get(n, {}))
            if cont:
                return cont.most_common(1)[0][0]
        return "?"

    nome_no = {n: nome_cell(n) for n in nos}
    cell_seg = {}
    for i, t in enumerate(trechos):
        for c in t[1:-1]:
            cell_seg[c] = i
    # hexágonos-nó pertencem ao trecho com mais viagens
    for i, t in enumerate(trechos):
        for c in (t[0], t[-1]):
            if c not in cell_seg or cell_v[c] > 0 and len(t) > len(trechos[cell_seg[c]]):
                cell_seg.setdefault(c, i)

    # 2. decompõe cada viagem em ponta de origem + troncos + ponta de destino
    tr_v = collections.Counter()
    tr_od = collections.defaultdict(set)
    tr_cli = collections.defaultdict(collections.Counter)
    tr_rotas = collections.defaultdict(set)
    for v in vs:
        v["troncos"], v["entrada"], v["saida"] = [], None, None
        if not v["cells"]:
            continue
        seq = percorrer(v["cells"], cell_seg)
        if not seq:
            continue
        ids = [s[0] for s in seq]
        v["troncos"] = ids
        t0, tN = trechos[seq[0][0]], trechos[seq[-1][0]]
        # entrada = nó do 1º trecho mais perto de onde a viagem entrou; saída idem
        pos0, posN = t0.index(seq[0][1]), tN.index(seq[-1][2])
        v["entrada"] = t0[0] if pos0 <= len(t0) - 1 - pos0 else t0[-1]
        v["saida"] = tN[0] if posN <= len(tN) - 1 - posN else tN[-1]
        if len(seq) > 1:  # direção conhecida pelo trecho seguinte
            v["entrada"] = t0[0] if t0[-1] in trechos[seq[1][0]] else t0[-1] if t0[0] in trechos[seq[1][0]] else v["entrada"]
            v["saida"] = tN[-1] if tN[0] in trechos[seq[-2][0]] else tN[0] if tN[-1] in trechos[seq[-2][0]] else v["saida"]
        for i in set(ids):
            tr_v[i] += 1
            tr_od[i].add((v["orig"], v["dest"]))
            tr_cli[i][v["cliente"]] += 1
            tr_rotas[i].add(v["rota"])

    # 3. tabelas de saída
    n_meses = len(ms)
    T = []
    for i, t in enumerate(trechos):
        ll = suavizar([h3.cell_to_latlng(c) for c in t])
        T.append(dict(id=i, a=nome_no.get(t[0], "?"), b=nome_no.get(t[-1], "?"),
                      no_a=t[0], no_b=t[-1],
                      km=round(sum(km(p, q) for p, q in zip(ll, ll[1:]))),
                      viagens=tr_v[i], viagens_mes=round(tr_v[i] / n_meses), pares_od=len(tr_od[i]),
                      rotas=len(tr_rotas[i]), clientes=tr_cli[i].most_common(5),
                      via=[nome_cell(c, 2) for c in t[:: max(1, len(t) // 6)]],
                      geo=[[round(a, 4), round(b, 4)] for a, b in ll]))
    # nomeia troncos T001.. por volume
    ordem = sorted(range(len(T)), key=lambda i: -T[i]["viagens"])
    cod = {i: f"T{k + 1:03d}" for k, i in enumerate(ordem)}
    for t in T:
        t["codigo"] = cod[t["id"]]
        via = [x for x in dict.fromkeys(t["via"]) if x not in ("?", t["a"], t["b"])][:3]
        t["nome"] = f"{t['a'].split('/')[0].title()} ↔ {t['b'].split('/')[0].title()}" + (
            f" (via {', '.join(x.split('/')[0].title() for x in via)})" if via else "")
        del t["via"]

    N = [dict(id=n, nome=nome_no[n], lat=round(h3.cell_to_latlng(n)[0], 4), lng=round(h3.cell_to_latlng(n)[1], 4),
              tipo="acesso" if n in extra else ("bifurcação" if len(adj[n]) >= 3 else "ponta de linha"),
              grau=len(adj[n])) for n in nos]
    nid = {n["id"]: f"N{k + 1:03d}" for k, n in enumerate(sorted(N, key=lambda n: -acesso.get(n["id"], 0)))}
    for n in N:
        n["codigo"] = nid[n["id"]]
        n["acessos"] = acesso.get(n["id"], 0)

    # rotas padrão = origem -> [troncos] -> destino (via dominante do par)
    pares = collections.defaultdict(list)
    for v in vs:
        pares[(v["orig"], v["dest"])].append(v)
    R = []
    pontas_o, pontas_d = collections.Counter(), collections.Counter()
    for (o, d), lst in pares.items():
        com = [v for v in lst if v["troncos"]]
        vias = collections.Counter(tuple(cod[i] for i in v["troncos"]) for v in com)
        kmc = lambda c: T[ordem[int(c[1:]) - 1]]["km"] or 1

        def cobre(via, viagem):  # % do km da via padrão que a viagem percorreu
            s = set(viagem)
            return sum(kmc(c) for c in via if c in s) / sum(kmc(c) for c in via)

        via, n_via = ((), 0)
        if vias:
            cands = [v for v, _ in vias.most_common(6)]
            via = max(cands, key=lambda c: sum(n * (cobre(c, o) + cobre(o, c)) for o, n in vias.items()))
            n_via = sum(n for o, n in vias.items() if cobre(via, o) >= 0.8 and cobre(o, via) >= 0.8)
        ref = [v for v in com if tuple(cod[i] for i in v["troncos"]) == via]
        ent = collections.Counter(nid[v["entrada"]] for v in ref).most_common(1)[0][0] if ref else ""
        sai = collections.Counter(nid[v["saida"]] for v in ref).most_common(1)[0][0] if ref else ""
        if via:
            pontas_o[(o, ent)] += len(lst)
            pontas_d[(sai, d)] += len(lst)
        ader = [v["aderencia"] for v in lst if "aderencia" in v]
        R.append(dict(
            origem=o, destino=d, viagens=len(lst), viagens_mes=round(len(lst) / n_meses, 1),
            rotas_cadastradas=len({v["rota"] for v in lst}),
            codigos_rota=sorted({v["rota"] for v in lst}),
            entrada=ent, troncos=list(via), saida=sai,
            km_tronco=sum(T[ordem[int(c[1:]) - 1]]["km"] for c in via),
            km_plan=round(sorted(v["km_plan"] or 0 for v in lst)[len(lst) // 2]),
            pct_via_padrao=round(100 * n_via / len(com)) if com else None,
            vias_alternativas=len(vias),
            clientes=collections.Counter(v["cliente"] for v in lst).most_common(3),
            vazio=round(100 * sum(v["vazio"] for v in lst) / len(lst)),
            pct_gps=round(100 * sum(v["fonte"] == "gps" for v in lst) / len(lst)),
            aderencia_plan=round(sum(ader) / len(ader)) if ader else None,
            descricoes=collections.Counter(v["rota_desc"] for v in lst).most_common(3),
            o_ll=[round(x, 4) for x in lst[0]["o_ll"]], d_ll=[round(x, 4) for x in lst[0]["d_ll"]],
        ))
    R.sort(key=lambda r: -r["viagens"])

    P = [dict(tipo="origem", cidade=o, no=n, viagens=c) for (o, n), c in pontas_o.items()] + \
        [dict(tipo="destino", cidade=d, no=n, viagens=c) for (n, d), c in pontas_d.items()]

    kpi = dict(
        periodo=f"{ini} a {fim}", meses=n_meses, viagens=len(vs),
        viagens_gps=sum(v["fonte"] == "gps" for v in vs),
        viagens_plan=sum(v["fonte"] == "planejada" for v in vs),
        viagens_sem_trilha=sum(v["fonte"] is None for v in vs),
        viagens_com_tronco=sum(bool(v["troncos"]) for v in vs),
        rotas_cadastradas=len({v["rota"] for v in vs}), pares_od=len(pares),
        troncos=len(T), nos=len(N), pontas_origem=len(pontas_o), pontas_destino=len(pontas_d),
        rotas_padrao=sum(1 for r in R if r["troncos"]), pares_sem_tronco=sum(1 for r in R if not r["troncos"]),
        motivos_sem_gps=dict(motivos), placas=len({v["placa"] for v in vs}),
        placas_sem_gps_detalhe=placas_sem_gps(vs, ms),
        gerado_em=datetime.now().strftime("%d/%m/%Y %H:%M"),
    )
    # fluxo por hexágono (mapa de calor), só o que tem volume
    H = [[c, cell_v[c], len(cell_od[c])] for c in cell_v if cell_v[c] >= 10]
    H = [[round(h3.cell_to_latlng(c)[0], 3), round(h3.cell_to_latlng(c)[1], 3), n, od] for c, n, od in H]
    out = dict(kpi=kpi, troncos=T, nos=N, rotas=R, pontas=P, calor=H)
    for t in T:
        t["no_a"], t["no_b"] = nid[t["no_a"]], nid[t["no_b"]]
    for n in N:
        n["id"] = n.pop("codigo")
    salvar(out, os.path.join(SAIDA, "malha.json.gz"))
    kpi["placas_sem_gps"] = [p["placa"] for p in kpi["placas_sem_gps_detalhe"]]
    exportar_excel(out, os.path.join(SAIDA, "malha.xlsx"))
    print({k: v for k, v in kpi.items() if not k.startswith("placas_sem")})


def placas_sem_gps(vs, ms):
    """Placas com viagem no período e sem nenhum ponto no banco histórico de GPS (arquivo vazio)."""
    tem = collections.defaultdict(list)
    for y, m in ms:
        for fn in glob.glob(os.path.join(DATA, "gps", f"{y}-{m:02d}", "*.json.gz")):
            if os.path.getsize(fn) > 60:
                tem[os.path.basename(fn)[:-8]].append(f"{y}-{m:02d}")
    por = collections.defaultdict(list)
    for v in vs:
        if v["placa"]:
            por[v["placa"]].append(v)
    out = []
    for p, lst in por.items():
        if len(tem.get(p, [])) < len(ms):
            out.append(dict(placa=p, viagens=len(lst), meses_com_gps=len(tem.get(p, [])), meses_no_periodo=len(ms),
                            cliente_principal=collections.Counter(v["cliente"] for v in lst).most_common(1)[0][0]))
    out.sort(key=lambda r: (r["meses_com_gps"], -r["viagens"]))
    return out


def exportar_excel(out, caminho):
    from openpyxl import Workbook
    from openpyxl.styles import Font
    wb = Workbook()
    ws = wb.active
    ws.title = "Resumo"
    k = out["kpi"]
    for linha in [("Período", k["periodo"]), ("Viagens analisadas", k["viagens"]),
                  ("Rotas cadastradas usadas", k["rotas_cadastradas"]), ("Pares cidade→cidade (rotas padrão)", k["pares_od"]),
                  ("Troncos", k["troncos"]), ("Nós", k["nos"]), ("Pontas de origem", k["pontas_origem"]),
                  ("Pontas de destino", k["pontas_destino"]), ("Viagens com GPS realizado", k["viagens_gps"]),
                  ("Viagens pela rota planejada", k["viagens_plan"]), ("Viagens sem trilha", k["viagens_sem_trilha"]),
                  ("Viagens que usam tronco", k["viagens_com_tronco"])]:
        ws.append(linha)

    def aba(nome, cab, linhas):
        w = wb.create_sheet(nome)
        w.append(cab)
        for c in w[1]:
            c.font = Font(bold=True)
        for l in linhas:
            w.append(l)
        w.freeze_panes = "A2"
        w.auto_filter.ref = w.dimensions
        for col in w.columns:
            w.column_dimensions[col[0].column_letter].width = min(60, max(10, max(len(str(c.value or "")) for c in col[:200]) + 2))

    aba("Rotas padrão", ["Origem", "Destino", "Nó entrada", "Troncos", "Nó saída", "Viagens", "Viagens/mês",
                         "Rotas cadastradas hoje", "Códigos de rota", "% na via padrão", "Vias diferentes", "km planejado",
                         "km em tronco", "% GPS", "Aderência ao planejado %", "% vazio", "Clientes"],
        [[r["origem"], r["destino"], r["entrada"], " > ".join(r["troncos"]), r["saida"], r["viagens"], r["viagens_mes"],
          r["rotas_cadastradas"], ", ".join(map(str, r["codigos_rota"])), r["pct_via_padrao"], r["vias_alternativas"],
          r["km_plan"], r["km_tronco"], r["pct_gps"], r["aderencia_plan"], r["vazio"],
          ", ".join(c[0] for c in r["clientes"])] for r in out["rotas"]])
    aba("Troncos", ["Código", "Tronco", "Nó A", "Nó B", "km", "Viagens", "Viagens/mês", "Pares O/D", "Rotas cadastradas", "Clientes"],
        [[t["codigo"], t["nome"], t["no_a"], t["no_b"], t["km"], t["viagens"], t["viagens_mes"], t["pares_od"], t["rotas"],
          ", ".join(c[0] for c in t["clientes"])] for t in sorted(out["troncos"], key=lambda t: t["codigo"])])
    aba("Nós", ["Nó", "Local", "Tipo", "Lat", "Lng", "Entradas/saídas"],
        [[n["id"], n["nome"], n["tipo"], n["lat"], n["lng"], n["acessos"]] for n in sorted(out["nos"], key=lambda n: n["id"])])
    aba("Pontas", ["Tipo", "Cidade", "Nó", "Viagens"],
        [[p["tipo"], p["cidade"], p["no"], p["viagens"]] for p in sorted(out["pontas"], key=lambda p: -p["viagens"])])
    sem = k["placas_sem_gps_detalhe"]
    aba("Placas sem GPS", ["Placa", "Viagens no período", "Meses com GPS", "Meses no período", "Cliente principal"],
        [[p["placa"], p["viagens"], p["meses_com_gps"], p["meses_no_periodo"], p["cliente_principal"]] for p in sem])
    wb.save(caminho)


if __name__ == "__main__":
    main()
