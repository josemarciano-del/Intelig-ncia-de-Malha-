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
            placa=x["veiculo"]["placa"], rastreador=x["veiculo"]["rastreador"] or "", status=x["status"]["descricao"], finalizada=x["status"]["finalizada"],
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


HUB_KM = 20             # nós ligados por trecho < 20 km, ou a < 15 km em linha reta, viram um único polo
MIN_TRECHO_MES = 2      # trecho com < 2 viagens/mês sai da malha (é ruído do GPS/traçado)
ANG_RETO = 125          # corredor só atravessa um polo se a estrada segue "reta" (ângulo entre trechos >= 125°)
PASSO_WP = 14           # um ponto de passagem para o roteirizador a cada ~14 hexágonos (~90 km)
DIAM_POLO = 30          # um polo não passa de 30 km de diâmetro (evita "polo Grande SP" inteiro)


def centro(c):
    return h3.cell_to_latlng(c)


def comprimento(cells):
    return sum(km(centro(a), centro(b)) for a, b in zip(cells, cells[1:]))


def pontos_estrada(rotas):
    """Para cada hexágono, um ponto que fica numa estrada de fato usada (traçado planejado das rotas)."""
    pe = {}
    for cod in sorted(rotas):
        for la, lo in decode(rotas[cod]["polilinha"])[::2]:
            pe.setdefault(h3.latlng_to_cell(la, lo, RES), (la, lo))
    return pe


def polos(trechos, nos, acesso):
    pai = {n: n for n in nos}

    def raiz(x):
        while pai[x] != x:
            pai[x] = pai[pai[x]]
            x = pai[x]
        return x

    membros = {n: [n] for n in nos}

    def unir(a, b):
        ra, rb = raiz(a), raiz(b)
        if ra == rb or any(km(centro(x), centro(y)) > DIAM_POLO for x in membros[ra] for y in membros[rb]):
            return
        novo, velho = min(ra, rb), max(ra, rb)
        pai[velho] = novo
        membros[novo] = membros[novo] + membros.pop(velho)

    for t in sorted(trechos, key=comprimento):
        if t[0] != t[-1] and comprimento(t) < HUB_KM:
            unir(t[0], t[-1])
    lst = sorted(nos)
    for i, a in enumerate(lst):
        for b in lst[i + 1:]:
            if km(centro(a), centro(b)) < 15:
                unir(a, b)
    grupos = collections.defaultdict(list)
    for n in lst:
        grupos[raiz(n)].append(n)
    hub = {}
    for g in grupos.values():
        rep = max(g, key=lambda n: (acesso.get(n, 0), n))
        for n in g:
            hub[n] = rep
    return hub


def decompor(vs, trechos, ativos, hub):
    """Sequência de trechos (só os ativos) de cada viagem + polo de entrada e de saída."""
    cell_seg = {}
    for i in sorted(ativos, key=lambda i: -len(trechos[i])):
        for c in trechos[i]:
            cell_seg.setdefault(c, i)
    for v in vs:
        v["troncos"], v["entrada"], v["saida"] = [], None, None
        if not v["cells"]:
            continue
        seq = percorrer(v["cells"], cell_seg)
        if not seq:
            continue
        ends = lambda s: (hub[trechos[s][0]], hub[trechos[s][-1]])
        ids = [s[0] for s in seq]
        a0, b0 = ends(ids[0])
        aN, bN = ends(ids[-1])
        if len(ids) > 1:
            prox, ant = set(ends(ids[1])), set(ends(ids[-2]))
            ent = a0 if b0 in prox else b0 if a0 in prox else None
            sai = bN if aN in ant else aN if bN in ant else None
        else:
            ent = sai = None
        t0 = trechos[ids[0]]
        if ent is None:
            p = t0.index(seq[0][1])
            ent = a0 if p <= len(t0) - 1 - p else b0
        if sai is None:
            sai = (bN if ent == aN else aN) if len(ids) == 1 else (aN if trechos[ids[-1]].index(seq[-1][2]) <= len(trechos[ids[-1]]) // 2 else bN)
        v["troncos"], v["entrada"], v["saida"] = ids, ent, sai


def angulo(trechos, a, b, h, hub):
    """Ângulo (graus) entre os trechos a e b no polo h: 180 = estrada reta, 0 = volta."""
    def saida(s):
        t = trechos[s]
        t = t if hub[t[0]] == h else t[::-1]
        p0, p1 = centro(t[0]), centro(t[min(len(t) - 1, 5)])
        return ((p1[1] - p0[1]) * math.cos(math.radians(p0[0])), p1[0] - p0[0])
    va, vb = saida(a), saida(b)
    na, nb = math.hypot(*va), math.hypot(*vb)
    if na * nb == 0:
        return 180
    return math.degrees(math.acos(max(-1, min(1, (va[0] * vb[0] + va[1] * vb[1]) / (na * nb)))))


def montar_corredores(trechos, ativos, vs, tr_v, hub):
    """Une trechos consecutivos quando o fluxo atravessa o polo em linha reta. Devolve lista de
    corredores, cada um como [(trecho, invertido)], e a sequência de polos do corredor."""
    ends = lambda s: (hub[trechos[s][0]], hub[trechos[s][-1]])
    passa = collections.Counter()
    for v in vs:
        for a, b in zip(v["troncos"], v["troncos"][1:]):
            if a != b:
                passa[(min(a, b), max(a, b))] += 1
    usado, liga = set(), collections.defaultdict(dict)
    pai = {s: s for s in ativos}

    def raiz(i):
        while pai[i] != i:
            pai[i] = pai[pai[i]]
            i = pai[i]
        return i

    for (a, b), n in sorted(passa.items(), key=lambda kv: (-kv[1], kv[0])):
        if a not in pai or b not in pai:
            continue
        comum = set(ends(a)) & set(ends(b))
        if len(comum) != 1 or n < 0.6 * min(tr_v[a], tr_v[b]) or n < 0.4 * max(tr_v[a], tr_v[b]):
            continue
        h = comum.pop()
        if (a, h) in usado or (b, h) in usado or raiz(a) == raiz(b) or angulo(trechos, a, b, h, hub) < ANG_RETO:
            continue
        usado |= {(a, h), (b, h)}
        liga[a][h], liga[b][h] = b, a
        pai[raiz(a)] = raiz(b)
    out, feito = [], set()
    for s in sorted(ativos):
        if s in feito or len(liga[s]) == 2:
            continue
        a, b = ends(s)
        inv = a in liga[s]          # a ponta ligada fica no fim
        cadeia, polos_c = [(s, inv)], [b, a] if inv else [a, b]
        feito.add(s)
        cur = s
        while polos_c[-1] in liga[cur]:
            nxt = liga[cur][polos_c[-1]]
            na, nb = ends(nxt)
            inv2 = nb == polos_c[-1]
            cadeia.append((nxt, inv2))
            polos_c.append(na if inv2 else nb)
            feito.add(nxt)
            cur = nxt
        out.append((cadeia, polos_c))
    for s in sorted(ativos):     # anéis fechados: cada trecho sozinho
        if s not in feito:
            out.append(([(s, False)], list(ends(s))))
    return out


def dividir_sinuosos(corr, trechos, hub, fator=1.4):
    """Quebra corredores que serpenteiam: um corredor precisa ir razoavelmente reto de ponta a ponta
    (caminho <= 1,4 x a distância entre as pontas, quando passa de 150 km)."""
    out = []
    for cad, pol in corr:
        atual, pol_at, comp = [], [pol[0]], 0.0
        for k, (s, inv) in enumerate(cad):
            c = comprimento(trechos[s])
            reta = km(centro(pol_at[0]), centro(pol[k + 1]))
            if atual and comp + c > 150 and comp + c > fator * reta:
                out.append((atual, pol_at))
                atual, pol_at, comp = [], [pol[k]], 0.0
            atual.append((s, inv))
            pol_at.append(pol[k + 1])
            comp += c
        out.append((atual, pol_at))
    return out


def unir_mesma_rodovia(G, ang_min=145):
    """Une corredores vizinhos que são a mesma rodovia seguindo reto pelo polo (ex.: BR-101 inteira)."""
    def rod(g):
        tot = sum(g["refs"].values())
        r, q = (g["refs"].most_common(1) or [(None, 0)])[0]
        return r if tot and q >= 0.4 * tot else None

    def direcao(g, h):
        ll = g["ll"] if g["pol"][0] == h else g["ll"][::-1]
        p0, acc = ll[0], 0
        for a, b in zip(ll, ll[1:]):
            acc += km(a, b)
            if acc >= 20:
                return ((b[1] - p0[1]) * math.cos(math.radians(p0[0])), b[0] - p0[0])
        b = ll[-1]
        return ((b[1] - p0[1]) * math.cos(math.radians(p0[0])), b[0] - p0[0])

    cand = []
    por_polo = collections.defaultdict(list)
    for i, g in enumerate(G):
        for h in {g["pol"][0], g["pol"][-1]}:
            por_polo[h].append(i)
    for h in sorted(por_polo):
        ids = por_polo[h]
        for x in range(len(ids)):
            for y in range(x + 1, len(ids)):
                i, j = ids[x], ids[y]
                if rod(G[i]) and rod(G[i]) == rod(G[j]) and G[i]["pol"][0] != G[i]["pol"][-1] and G[j]["pol"][0] != G[j]["pol"][-1]:
                    va, vb = direcao(G[i], h), direcao(G[j], h)
                    na, nb = math.hypot(*va), math.hypot(*vb)
                    if na * nb and math.degrees(math.acos(max(-1, min(1, (va[0] * vb[0] + va[1] * vb[1]) / (na * nb))))) >= ang_min:
                        cand.append((-(G[i]["km"] + G[j]["km"]), h, i, j))
    usado, liga, pai = set(), collections.defaultdict(dict), list(range(len(G)))

    def raiz(i):
        while pai[i] != i:
            pai[i] = pai[pai[i]]
            i = pai[i]
        return i

    for _, h, i, j in sorted(cand):
        if (i, h) in usado or (j, h) in usado or raiz(i) == raiz(j):
            continue
        usado |= {(i, h), (j, h)}
        liga[i][h], liga[j][h] = j, i
        pai[raiz(i)] = raiz(j)
    out, feito = [], set()
    for i in range(len(G)):
        if i in feito or len(liga[i]) == 2:
            continue
        g = G[i]
        inv = g["pol"][0] in liga[i]
        cadeia = [(i, inv)]
        fim = g["pol"][0] if inv else g["pol"][-1]
        feito.add(i)
        cur = i
        while fim in liga[cur]:
            nxt = liga[cur][fim]
            inv2 = G[nxt]["pol"][-1] == fim
            cadeia.append((nxt, inv2))
            fim = G[nxt]["pol"][0] if inv2 else G[nxt]["pol"][-1]
            feito.add(nxt)
            cur = nxt
        m = dict(cad=[], pol=[], ll=[], km=0.0, refs=collections.Counter(), estrada=True)
        for k, invk in cadeia:
            gk = G[k]
            cad = [(s, not v) for s, v in reversed(gk["cad"])] if invk else gk["cad"]
            pol = gk["pol"][::-1] if invk else gk["pol"]
            ll = gk["ll"][::-1] if invk else gk["ll"]
            m["cad"] += cad
            m["pol"] += pol if not m["pol"] else pol[1:]
            m["ll"] += ll if not m["ll"] else ll[1:]
            m["km"] += gk["km"]
            m["refs"].update(gk["refs"])
            m["estrada"] = m["estrada"] and gk["estrada"]
        out.append(m)
    for i in range(len(G)):
        if i not in feito:
            out.append(G[i])
    return out


def caminho_km(grafo, ent, sai, permitidos=None):
    """Menor km entre dois polos pela malha (opcionalmente só pelos troncos permitidos)."""
    import heapq
    dist, fila = {ent: 0.0}, [(0.0, ent)]
    while fila:
        d, n = heapq.heappop(fila)
        if n == sai:
            return d
        if d > dist.get(n, 1e18):
            continue
        for m, k, t in grafo.get(n, ()):
            if permitidos and t not in permitidos:
                continue
            nd = d + k
            if nd < dist.get(m, 1e18):
                dist[m] = nd
                heapq.heappush(fila, (nd, m))
    return None


PESO_PONTA = 1.7         # 1 km de ponta "custa" 1,7 km de tronco (prioridade ao tronco)
TROCA_KM = 25            # trocar de tronco custa o equivalente a 25 km
BONUS_HIST = 0.85        # troncos que o par já usa na prática ficam 15% mais baratos
MAX_SOBRE_DIRETO = 1.25  # se a rota pela malha passar de 1,25 x a ligação direta, o par é ligação direta
RAIO_PONTA = 220


def melhor_rota(grafo, pos_no, o_ll, d_ll, fator, hist=(), pontas_km=None):
    """Menor custo: pontas (km x PESO_PONTA) + troncos (km, com bônus no histórico) + trocas de tronco.
    Devolve (entrada, [troncos], saída, km_ponta_o, km_tronco, km_ponta_d) ou None."""
    import heapq
    pontas_km = pontas_km or {}
    def cands(ll, tipo):
        lst = sorted(((km(ll, p), n) for n, p in pos_no.items() if n in grafo), key=lambda x: (x[0], x[1]))
        out = []
        for d, n in [x for x in lst if x[0] <= RAIO_PONTA][:12]:
            k = pontas_km.get((tipo, n)) or d * fator
            out.append((n, k))
        return out
    oc, dc = cands(o_ll, "o"), dict(cands(d_ll, "d"))
    if not oc or not dc:
        return None
    hist = set(hist)
    dist, prev, fila = {}, {}, []
    for n, k in oc:
        st = (n, "")
        c = k * PESO_PONTA
        if c < dist.get(st, 1e18):
            dist[st], prev[st] = c, ("ini", k)
            heapq.heappush(fila, (c, st))
    best = None
    while fila:
        c, st = heapq.heappop(fila)
        if c > dist.get(st, 1e18):
            continue
        if best and c >= best[0]:
            break
        n, lt = st
        if n in dc:
            tot = c + dc[n] * PESO_PONTA
            if not best or tot < best[0]:
                best = (tot, st)
        for m, k, t in grafo.get(n, ()):
            nc = c + k * (BONUS_HIST if t in hist else 1) + (TROCA_KM if lt and lt != t else 0)
            ns = (m, t)
            if nc < dist.get(ns, 1e18):
                dist[ns], prev[ns] = nc, (st, k)
                heapq.heappush(fila, (nc, ns))
    if not best:
        return None
    st, troncos, kmt = best[1], [], 0.0
    while True:
        p = prev[st]
        if p[0] == "ini":
            ent, kpo = st[0], p[1]
            break
        kmt += p[1]
        if not troncos or troncos[-1] != st[1]:
            troncos.append(st[1])
        st = p[0]
    troncos.reverse()
    sai = best[1][0]
    return ent, troncos, sai, kpo, kmt, dc[sai]


def tirar_espinhos(ll):
    """Remove 'espinhos' do traçado (retorno em U do roteirizador num ponto de passagem)."""
    mudou = True
    while mudou and len(ll) > 3:
        mudou = False
        out = [ll[0]]
        for i in range(1, len(ll) - 1):
            a, b, c = out[-1], ll[i], ll[i + 1]
            v1 = ((b[1] - a[1]) * math.cos(math.radians(a[0])), b[0] - a[0])
            v2 = ((c[1] - b[1]) * math.cos(math.radians(b[0])), c[0] - b[0])
            n1, n2 = math.hypot(*v1), math.hypot(*v2)
            if n1 * n2 and (v1[0] * v2[0] + v1[1] * v2[1]) / (n1 * n2) < -0.85 and min(km(a, b), km(b, c)) < 8:
                mudou = True
                continue
            out.append(b)
        out.append(ll[-1])
        ll = out
    return ll


def geometria(cells, pe):
    """Traçado por estrada (OSRM) passando por pontos de estrada amostrados ao longo da cadeia de hexágonos."""
    from shapely.geometry import LineString
    from .estrada import rota_limpa
    idx = list(range(0, len(cells), PASSO_WP))
    if idx[-1] != len(cells) - 1:
        idx.append(len(cells) - 1)
    wps = [pe.get(cells[i]) or centro(cells[i]) for i in idx]
    r = rota_limpa(wps) if len(wps) >= 2 else None
    if not r or r["km"] > 1.6 * comprimento(cells) + 20:
        ll = suavizar([centro(c) for c in cells])
        return ll, comprimento(cells), collections.Counter(), False
    ls = LineString([(lo, la) for la, lo in r["coords"]]).simplify(0.0025, preserve_topology=False)
    ll = tirar_espinhos([(la, lo) for lo, la in ls.coords])
    return ll, r["km"], r["refs"], True


def projetar(ll, acum, p):
    i = min(range(len(ll)), key=lambda k: (ll[k][0] - p[0]) ** 2 + ((ll[k][1] - p[1]) * math.cos(math.radians(p[0]))) ** 2)
    return i, acum[i], km(ll[i], p)


def main():
    # resultado determinístico: a ordem de sets/dicts de strings depende do hash; sem isso os códigos T/N mudam a cada rodada
    if os.environ.get("PYTHONHASHSEED") != "0":
        os.execve(sys.executable, [sys.executable, "-m", "malha.processa", *sys.argv[1:]], {**os.environ, "PYTHONHASHSEED": "0"})
    from .estrada import matriz
    ini, fim = sys.argv[1], sys.argv[2]
    ms = list(meses(ini, fim))
    n_meses = len(ms)
    vs = carregar_viagens(ini, fim)
    print("viagens com etapas:", len(vs), flush=True)
    motivos, rotas = trilhas(vs, ms)
    adj, cell_v, cell_od, nomes = construir_malha(vs, n_meses)
    pe = pontos_estrada(rotas)

    # 1. trechos entre bifurcações; pontos de entrada/saída frequentes viram nós
    trechos, nos = segmentar(adj, set())
    acesso = collections.Counter()
    for v in vs:
        tc = [c for c in (v["cells"] or []) if c in adj]
        if tc:
            acesso[tc[0]] += 1
            acesso[tc[-1]] += 1
    extra = set()
    for c, n in sorted(acesso.items(), key=lambda kv: (-kv[1], kv[0])):
        if n < MIN_ACESSO_MES * n_meses:
            break
        if c in nos or any(km(centro(c), centro(e)) < 25 for e in extra | nos):
            continue
        extra.add(c)
    trechos, nos = segmentar(adj, extra)
    trechos.sort(key=lambda t: (t[0], t[-1], len(t)))

    # 2. polos: nós colados viram um só; trechos internos ao polo saem da malha
    hub = polos(trechos, nos, acesso)
    ativos = {i for i, t in enumerate(trechos) if hub[t[0]] != hub[t[-1]]}
    decompor(vs, trechos, ativos, hub)
    tr_v = collections.Counter(s for v in vs for s in set(v["troncos"]))
    ativos = {i for i in ativos if tr_v[i] >= MIN_TRECHO_MES * n_meses}
    decompor(vs, trechos, ativos, hub)
    tr_v = collections.Counter(s for v in vs for s in set(v["troncos"]))
    print(f"hexágonos tronco: {len(adj)} | trechos: {len(trechos)} -> ativos {len(ativos)} | polos: {len(set(hub.values()))}", flush=True)

    # 3. corredores (troncos) pela continuidade da estrada, com traçado real (OSRM)
    corr = dividir_sinuosos(montar_corredores(trechos, ativos, vs, tr_v, hub), trechos, hub)
    G = []
    for k, (cad, pol) in enumerate(corr):
        cells = []
        for s_, inv in cad:
            t = trechos[s_][::-1] if inv else trechos[s_]
            cells += t if not cells else t[1:]
        ll, km_real, refs, estrada = geometria(cells, pe)
        G.append(dict(cad=cad, pol=pol, ll=ll, km=km_real, refs=refs, estrada=estrada))
        if k % 50 == 0:
            print(f"  traçado {k}/{len(corr)}", flush=True)
    G = unir_mesma_rodovia(G)
    mapa = {s_: k for k, g in enumerate(G) for s_, _ in g["cad"]}
    tr_v = collections.Counter()
    tr_od, tr_cli, tr_rotas = collections.defaultdict(set), collections.defaultdict(collections.Counter), collections.defaultdict(set)
    for v in vs:
        ids = []
        for s_ in v["troncos"]:
            if not ids or ids[-1] != mapa[s_]:
                ids.append(mapa[s_])
        v["troncos"] = ids
        for i in set(ids):
            tr_v[i] += 1
            tr_od[i].add((v["orig"], v["dest"]))
            tr_cli[i][v["cliente"]] += 1
            tr_rotas[i].add(v["rota"])
    print("corredores:", len(corr), "-> após unir mesma rodovia:", len(G), flush=True)

    # 4. nomes e posição dos polos
    lugares = Lugares(nomes)
    usados = sorted({h for g in G for h in g["pol"]})
    pos_polo = {h: pe.get(h) or centro(h) for h in usados}
    nome_polo = {h: lugares.nome(pos_polo[h], h) for h in usados}

    # 5. troncos: posição dos polos ao longo do traçado, rodovias e nome
    T = []
    for k, g in enumerate(G):
        ll, km_real, refs, pol = g["ll"], g["km"], g["refs"], g["pol"]
        acum = [0.0]
        for p, q in zip(ll, ll[1:]):
            acum.append(acum[-1] + km(p, q))
        esc = km_real / acum[-1] if acum[-1] else 1
        acum = [x * esc for x in acum]
        lin = []
        for h in dict.fromkeys(pol):
            i, d, dist = projetar(ll, acum, pos_polo[h])
            lin.append([h, round(d, 1), i])
        lin.sort(key=lambda x: x[1])
        tot = sum(refs.values()) or 1
        rod = [r if re.search(r"[A-Za-z]", r) else f"Ruta {r}" for r, q in refs.most_common(3) if q >= 0.2 * tot][:2]
        via = []
        for f in (0.33, 0.66):
            j = min(len(ll) - 1, int(len(ll) * f))
            n = lugares.nome(ll[j])
            if n not in ("?", nome_polo[pol[0]], nome_polo[pol[-1]]) and n not in via:
                via.append(n)
        T.append(dict(id=k, no_a=pol[0], no_b=pol[-1], nos_lin=lin, km=round(km_real), rodovias=rod, estrada=g["estrada"],
                      a=nome_polo[pol[0]], b=nome_polo[pol[-1]], via=via,
                      viagens=tr_v[k], viagens_mes=round(tr_v[k] / n_meses), pares_od=len(tr_od[k]),
                      rotas=len(tr_rotas[k]), clientes=tr_cli[k].most_common(5),
                      geo=[[round(a, 4), round(b, 4)] for a, b in ll]))
    ordem = sorted(range(len(T)), key=lambda i: (-T[i]["viagens"], T[i]["no_a"]))
    cod = {i: f"T{n + 1:03d}" for n, i in enumerate(ordem)}
    for t in T:
        t["codigo"] = cod[t["id"]]
        ab = f"Contorno de {t['a'].split('/')[0]}" if t["a"] == t["b"] else f"{t['a'].split('/')[0]} ↔ {t['b'].split('/')[0]}"
        t["nome"] = (" / ".join(t["rodovias"]) + " · " if t["rodovias"] else "") + ab + (
            f" (via {', '.join(x.split('/')[0] for x in t['via'])})" if t["via"] else "")
    grau = collections.Counter(h for t in T for h in {t["no_a"], t["no_b"]})
    ac_polo = collections.Counter()
    for c, n in acesso.items():
        if c in hub:
            ac_polo[hub[c]] += n
    for v in vs:
        if v["entrada"]:
            ac_polo[v["entrada"]] += 0
    nid = {h: f"N{n + 1:03d}" for n, h in enumerate(sorted(usados, key=lambda h: (-ac_polo[h], h)))}
    N = [dict(id=nid[h], nome=nome_polo[h], lat=round(pos_polo[h][0], 4), lng=round(pos_polo[h][1], 4), acessos=ac_polo[h], grau=grau[h],
              tipo="bifurcação" if grau[h] >= 3 else "ponta de linha" if grau[h] == 1 else "acesso" if ac_polo[h] >= MIN_ACESSO_MES * n_meses else "passagem")
         for h in usados]
    for t in T:
        t["no_a"], t["no_b"] = nid[t["no_a"]], nid[t["no_b"]]
        t["nos_lin"] = [[nid[h], d, i] for h, d, i in t["nos_lin"]]
    pos_no = {n["id"]: (n["lat"], n["lng"]) for n in N}
    lin_t = {t["codigo"]: {h: d for h, d, _ in t["nos_lin"]} for t in T}
    km_t = {t["codigo"]: t["km"] for t in T}
    grafo = collections.defaultdict(list)
    for t in T:
        for (a, da, _), (b, db, _) in zip(t["nos_lin"], t["nos_lin"][1:]):
            grafo[a].append((b, max(0.5, db - da), t["codigo"]))
            grafo[b].append((a, max(0.5, db - da), t["codigo"]))

    # 6. rotas padrão por par cidade -> cidade: melhor rota pela malha (mesmo motor do planejador),
    #    com bônus nos troncos que o par já usa; se não compensa frente à ligação direta, fica direta
    ll_cid = {}
    pares = collections.defaultdict(list)
    for v in vs:
        pares[(v["orig"], v["dest"])].append(v)
        ll_cid.setdefault(v["orig"], tuple(round(x, 4) for x in v["o_ll"]))
        ll_cid.setdefault(v["dest"], tuple(round(x, 4) for x in v["d_ll"]))
    fator0 = 1.29

    def cobre(via, viagem):
        st = set(viagem)
        return sum(km_t[c] or 1 for c in via if c in st) / max(1, sum(km_t[c] or 1 for c in via))

    def propor(o, d, lst, pkm=None):
        hist = collections.Counter(cod[i] for v in lst for i in set(v["troncos"]))
        usados = {c for c, n in hist.items() if n >= 0.3 * len(lst)}
        return melhor_rota(grafo, pos_no, ll_cid[o], ll_cid[d], fator0, usados, pkm)

    propostas = {par: propor(*par, lst) for par, lst in sorted(pares.items())}
    # km real das pontas escolhidas (OSRM), depois recalcula com esse km
    pont = collections.defaultdict(lambda: {"o": set(), "d": set()})
    for (o, d), p in propostas.items():
        if p:
            pont[p[0]]["o"].add(o)
            pont[p[2]]["d"].add(d)
    km_ponta = {}
    for no in sorted(pont):
        for tipo in ("o", "d"):
            cids = sorted(pont[no][tipo])
            if not cids:
                continue
            kms = matriz([ll_cid[c] for c in cids], [pos_no[no]]) if tipo == "o" else matriz([pos_no[no]], [ll_cid[c] for c in cids])
            for c, k in zip(cids, kms):
                km_ponta[(tipo, c, no)] = k
    fat = sorted(k / max(1, km(ll_cid[c], pos_no[n])) for (t, c, n), k in km_ponta.items() if k and km(ll_cid[c], pos_no[n]) > 5)
    fator_ponta = round(fat[len(fat) // 2], 3) if fat else fator0

    R, pontas_o, pontas_d, erros = [], collections.Counter(), collections.Counter(), []
    for (o, d), lst in sorted(pares.items()):
        p = propostas[(o, d)]
        if p:
            pk = {("o", p[0]): km_ponta.get(("o", o, p[0])), ("d", p[2]): km_ponta.get(("d", d, p[2]))}
            p = propor(o, d, lst, {k: v for k, v in pk.items() if v}) or p
        km_plan = round(sorted(v["km_plan"] or 0 for v in lst)[len(lst) // 2])
        direto = km_plan if km_plan > 30 else km(ll_cid[o], ll_cid[d]) * fator_ponta
        ent = sai = ""
        via, kpo, kmt, kpd = [], 0, 0, 0
        if p:
            kpo = km_ponta.get(("o", o, p[0])) or p[3]
            kpd = km_ponta.get(("d", d, p[2])) or p[5]
            if kpo + p[4] + kpd <= MAX_SOBRE_DIRETO * direto and p[1]:
                ent, via, sai, kmt = p[0], p[1], p[2], p[4]
        com = [v for v in lst if v["troncos"]]
        n_via = sum(1 for v in com if via and cobre(via, [cod[i] for i in v["troncos"]]) >= 0.8)
        if via:
            pontas_o[(o, ent)] += len(lst)
            pontas_d[(sai, d)] += len(lst)
            km_rota = round(kpo + kmt + kpd)
            if km_plan > 100:
                erros.append(km_rota / km_plan)
        else:
            km_rota = round(direto)
        ader = [v["aderencia"] for v in lst if "aderencia" in v]
        R.append(dict(
            origem=o, destino=d, viagens=len(lst), viagens_mes=round(len(lst) / n_meses, 1),
            rotas_cadastradas=len({v["rota"] for v in lst}), codigos_rota=sorted({v["rota"] for v in lst}),
            entrada=ent, troncos=list(via), saida=sai, km_tronco=round(kmt), km_rota=km_rota,
            km_ponta_o=round(kpo) if via else None, km_ponta_d=round(kpd) if via else None,
            km_plan=km_plan, tipo="malha" if via else "direta",
            pct_via_padrao=round(100 * n_via / len(com)) if com and via else None,
            vias_historicas=collections.Counter(tuple(cod[i] for i in v["troncos"]) for v in com).most_common(1)[0][0] if com else [],
            clientes=collections.Counter(v["cliente"] for v in lst).most_common(3),
            vazio=round(100 * sum(v["vazio"] for v in lst) / len(lst)),
            pct_gps=round(100 * sum(v["fonte"] == "gps" for v in lst) / len(lst)),
            aderencia_plan=round(sum(ader) / len(ader)) if ader else None,
            descricoes=collections.Counter(v["rota_desc"] for v in lst).most_common(3),
            o_ll=list(ll_cid[o]), d_ll=list(ll_cid[d]),
        ))
    R.sort(key=lambda r: (-r["viagens"], r["origem"], r["destino"]))
    # pontas que a 2ª passada escolheu e ainda não têm km por estrada
    falta = collections.defaultdict(lambda: {"o": set(), "d": set()})
    for r in R:
        if r["troncos"]:
            if ("o", r["origem"], r["entrada"]) not in km_ponta:
                falta[r["entrada"]]["o"].add(r["origem"])
            if ("d", r["destino"], r["saida"]) not in km_ponta:
                falta[r["saida"]]["d"].add(r["destino"])
    for no in sorted(falta):
        for tipo in ("o", "d"):
            cids = sorted(falta[no][tipo])
            if cids:
                kms = matriz([ll_cid[c] for c in cids], [pos_no[no]]) if tipo == "o" else matriz([pos_no[no]], [ll_cid[c] for c in cids])
                km_ponta.update({(tipo, c, no): k for c, k in zip(cids, kms)})
    for r in R:
        if r["troncos"]:
            r["km_ponta_o"] = round(km_ponta.get(("o", r["origem"], r["entrada"])) or r["km_ponta_o"])
            r["km_ponta_d"] = round(km_ponta.get(("d", r["destino"], r["saida"])) or r["km_ponta_d"])
            r["km_rota"] = r["km_ponta_o"] + r["km_tronco"] + r["km_ponta_d"]
    erros = sorted(r["km_rota"] / r["km_plan"] for r in R if r["troncos"] and r["km_plan"] > 100)
    P = [dict(tipo="origem", cidade=o, no=n, viagens=c, km=round(km_ponta[("o", o, n)], 1) if km_ponta.get(("o", o, n)) else None)
         for (o, n), c in pontas_o.items()] + \
        [dict(tipo="destino", cidade=d, no=n, viagens=c, km=round(km_ponta[("d", d, n)], 1) if km_ponta.get(("d", d, n)) else None)
         for (n, d), c in pontas_d.items()]
    P.sort(key=lambda p: (-p["viagens"], p["cidade"]))

    kpi = dict(
        periodo=f"{ini} a {fim}", meses=n_meses, viagens=len(vs),
        viagens_gps=sum(v["fonte"] == "gps" for v in vs),
        viagens_plan=sum(v["fonte"] == "planejada" for v in vs),
        viagens_sem_trilha=sum(v["fonte"] is None for v in vs),
        viagens_com_tronco=sum(r["viagens"] for r in R if r["troncos"]),
        rotas_cadastradas=len({v["rota"] for v in vs}), pares_od=len(pares),
        troncos=len(T), nos=len(N), pontas_origem=len(pontas_o), pontas_destino=len(pontas_d),
        rotas_padrao=sum(1 for r in R if r["troncos"]), pares_sem_tronco=sum(1 for r in R if not r["troncos"]),
        motivos_sem_gps=dict(motivos), placas=len({v["placa"] for v in vs}),
        placas_sem_gps_detalhe=placas_sem_gps(vs, ms), fator_ponta=fator_ponta,
        km_vs_planejado=[round(erros[int(len(erros) * q)], 3) for q in (.1, .25, .5, .75, .9)] if erros else None,
        troncos_por_estrada=sum(t["estrada"] for t in T),
        gerado_em=datetime.now().strftime("%d/%m/%Y %H:%M"),
    )
    H = [[c, cell_v[c], len(cell_od[c])] for c in sorted(cell_v) if cell_v[c] >= 10]
    H = [[round(centro(c)[0], 3), round(centro(c)[1], 3), n, od] for c, n, od in H]
    out = dict(kpi=kpi, troncos=T, nos=N, rotas=R, pontas=P, calor=H)
    salvar(out, os.path.join(SAIDA, "malha.json.gz"))
    kpi["placas_sem_gps"] = [p["placa"] for p in kpi["placas_sem_gps_detalhe"]]
    exportar_excel(out, os.path.join(SAIDA, "malha.xlsx"))
    print({k: v for k, v in kpi.items() if not k.startswith("placas_sem")})


class Lugares:
    """Nome do lugar: município IBGE (até 30 km) no Brasil; cidade Natural Earth (até 90 km) no exterior;
    por último, a referência do rastreador limpa."""

    def __init__(self, nomes_ref):
        import csv
        self.mun = Municipios()
        ref = os.path.join(os.path.dirname(os.path.abspath(__file__)), "ref")
        pais = {"ARG": "AR", "CHL": "CL", "URY": "UY", "PRY": "PY", "BOL": "BO", "PER": "PE"}
        self.ext = [(float(r["lat"]), float(r["lng"]), f"{r['nome']}/{pais[r['pais']]}", float(r["pop"] or 0))
                    for r in csv.DictReader(open(os.path.join(ref, "cidades_exterior.csv"), encoding="utf-8"))]
        self.ref = nomes_ref

    def nome(self, p, cell=None):
        n = self.mun.perto(p[0], p[1], 30)
        if n:
            return n
        best = min(self.ext, key=lambda e: km(p, e[:2]) - min(25, e[3] / 40000))
        if km(p, best[:2]) < 90:
            return best[2]
        if cell:
            cont = collections.Counter()
            for c in h3.grid_disk(cell, 2):
                cont.update(self.ref.get(c, {}))
            for nm, _ in cont.most_common():
                if not re.search(r"\d", nm):
                    return nm.title()
        return "?"


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
    import json
    fn = os.path.join(DATA, "placas_autotrac.json")
    at = json.load(open(fn)) if os.path.exists(fn) else {}
    out = []
    for p, lst in por.items():
        if len(tem.get(p, [])) < len(ms):
            a = at.get(p)
            situacao = ("não verificada" if a is None else "Autotrac, falha no histórico" if a["autotrac"]
                        else "fora da Autotrac")
            out.append(dict(placa=p, situacao=situacao, viagens=len(lst), meses_com_gps=len(tem.get(p, [])),
                            meses_no_periodo=len(ms),
                            rastreador=collections.Counter(v["rastreador"] for v in lst).most_common(1)[0][0],
                            ultima_viagem=max(v["criacao"] for v in lst),
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

    aba("Rotas padrão", ["Origem", "Destino", "Tipo", "Nó entrada", "Troncos", "Nó saída", "Cadastro padronizado",
                         "Viagens", "Viagens/mês", "Rotas cadastradas hoje", "Códigos de rota", "% viagens na via padrão",
                         "km cadastro", "km pela malha", "km ponta origem", "km em tronco", "km ponta destino",
                         "Via mais usada (histórico)", "% GPS", "% vazio", "Clientes"],
        [[r["origem"], r["destino"], r["tipo"], r["entrada"], " > ".join(r["troncos"]), r["saida"],
          " > ".join([r["origem"], r["entrada"], *r["troncos"], r["saida"], r["destino"]]) if r["troncos"] else f"{r['origem']} > {r['destino']} (direta)",
          r["viagens"], r["viagens_mes"], r["rotas_cadastradas"], ", ".join(map(str, r["codigos_rota"])), r["pct_via_padrao"],
          r["km_plan"], r["km_rota"], r["km_ponta_o"], r["km_tronco"], r["km_ponta_d"], " > ".join(r["vias_historicas"]),
          r["pct_gps"], r["vazio"], ", ".join(c[0] for c in r["clientes"])] for r in out["rotas"]])
    aba("Troncos", ["Código", "Tronco", "Rodovias", "Nó A", "Nó B", "km", "Viagens", "Viagens/mês", "Pares O/D", "Rotas cadastradas", "Clientes"],
        [[t["codigo"], t["nome"], " / ".join(t.get("rodovias", [])), t["no_a"], t["no_b"], t["km"], t["viagens"], t["viagens_mes"], t["pares_od"], t["rotas"],
          ", ".join(c[0] for c in t["clientes"])] for t in sorted(out["troncos"], key=lambda t: t["codigo"])])
    aba("Nós", ["Nó", "Local", "Tipo", "Lat", "Lng", "Entradas/saídas"],
        [[n["id"], n["nome"], n["tipo"], n["lat"], n["lng"], n["acessos"]] for n in sorted(out["nos"], key=lambda n: n["id"])])
    aba("Pontas", ["Tipo", "Cidade", "Nó", "km por estrada", "Viagens"],
        [[p["tipo"], p["cidade"], p["no"], p.get("km"), p["viagens"]] for p in sorted(out["pontas"], key=lambda p: -p["viagens"])])
    sem = k["placas_sem_gps_detalhe"]
    aba("Placas sem GPS", ["Placa", "Situação", "Rastreador (cadastro)", "Viagens no período", "Meses com GPS",
                           "Meses no período", "Última viagem", "Cliente principal"],
        [[p["placa"], p["situacao"], p["rastreador"], p["viagens"], p["meses_com_gps"], p["meses_no_periodo"],
          p["ultima_viagem"], p["cliente_principal"]] for p in sem])
    wb.save(caminho)


if __name__ == "__main__":
    main()
