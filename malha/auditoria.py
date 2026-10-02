"""Auditoria de qualidade da malha gerada (rodar após malha.processa).

Uso: python -m malha.auditoria   ->  imprime o relatório e grava data/saida/auditoria.txt
Cada verificação tem um limite; o relatório marca OK / ATENÇÃO.
"""
import collections
import math
import os
import statistics

from .api import ler
from .processa import SAIDA, km


def angulo_medio(geo):
    angs = []
    for a, b, c in zip(geo, geo[1:], geo[2:]):
        v1 = ((b[1] - a[1]) * math.cos(math.radians(a[0])), b[0] - a[0])
        v2 = ((c[1] - b[1]) * math.cos(math.radians(b[0])), c[0] - b[0])
        n1, n2 = math.hypot(*v1), math.hypot(*v2)
        if n1 * n2 > 0:
            angs.append(math.degrees(math.acos(max(-1, min(1, (v1[0] * v2[0] + v1[1] * v2[1]) / (n1 * n2))))))
    return statistics.mean(angs) if angs else 0


def retornos(geo):
    n = 0
    for a, b, c in zip(geo, geo[1:], geo[2:]):
        v1 = ((b[1] - a[1]) * math.cos(math.radians(a[0])), b[0] - a[0])
        v2 = ((c[1] - b[1]) * math.cos(math.radians(b[0])), c[0] - b[0])
        n1, n2 = math.hypot(*v1), math.hypot(*v2)
        if n1 * n2 and (v1[0] * v2[0] + v1[1] * v2[1]) / (n1 * n2) < -0.866:
            n += 1
    return n


def main():
    d = ler(os.path.join(SAIDA, "malha.json.gz"))
    T, R, N, P, K = d["troncos"], d["rotas"], d["nos"], d["pontas"], d["kpi"]
    rs = [r for r in R if r["troncos"]]
    linhas = []

    def chk(nome, valor, ok=True, detalhe=""):
        linhas.append(f"[{'OK' if ok else 'ATENÇÃO':7}] {nome}: {valor}{'  · ' + detalhe if detalhe else ''}")

    chk("Troncos", len(T))
    chk("Troncos com traçado por estrada", f"{sum(t.get('estrada', False) for t in T)} de {len(T)}",
        sum(t.get("estrada", False) for t in T) >= 0.95 * len(T))
    rev = sum(retornos(t["geo"]) for t in T)
    tot = sum(max(0, len(t["geo"]) - 2) for t in T)
    chk("Retornos em U no traçado (vértices com virada > 150°)", f"{rev} de {tot} ({100 * rev / max(1, tot):.1f}%)", rev <= 0.005 * tot)
    chk("Troncos sem viagem", sum(t["viagens_mes"] == 0 for t in T), all(t["viagens_mes"] > 0 for t in T))
    curtos = [t for t in T if t["km"] < 15]
    chk("Troncos < 15 km", len(curtos), len(curtos) <= 0.05 * len(T))
    mesmo = [t["codigo"] for t in T if t["no_a"] == t["no_b"] or t["a"] == t["b"]]
    chk("Contornos (tronco que começa e termina na mesma cidade)", len(mesmo), len(mesmo) <= 0.08 * len(T), " ".join(mesmo[:10]))
    sem_nome = [n["id"] for n in N if n["nome"] in ("?", "") or any(ch.isdigit() for ch in n["nome"])]
    chk("Nós sem nome válido", len(sem_nome), len(sem_nome) <= 0.02 * len(N), " ".join(sem_nome[:10]))
    chk("Maior tronco (km)", max(t["km"] for t in T), True, max(T, key=lambda t: t["km"])["nome"][:80])
    tpr = sorted(len(r["troncos"]) for r in rs)
    chk("Troncos por rota (mediana / p90)", f"{statistics.median(tpr)} / {tpr[int(.9 * len(tpr))]}", tpr[int(.9 * len(tpr))] <= 10)
    chk("Rotas com entrada = saída", sum(r["entrada"] == r["saida"] for r in rs), sum(r["entrada"] == r["saida"] for r in rs) <= 0.02 * len(rs))
    q = K.get("km_vs_planejado")
    if q:
        chk("km pela malha ÷ km do cadastro (p10 p25 mediana p75 p90)", q, 0.9 <= q[2] <= 1.12, "1,00 = igual ao cadastro")
    sem_km = sum(1 for p in P if p.get("km") is None)
    chk("Pontas sem km por estrada", sem_km, sem_km <= 0.05 * len(P))
    chk("Fator estrada ÷ linha reta nas pontas", K.get("fator_ponta"), 1.1 <= (K.get("fator_ponta") or 0) <= 1.6)
    peso = sum(t["viagens"] for t in T)
    pond = sum(r["pct_via_padrao"] * r["viagens"] for r in rs if r["pct_via_padrao"] is not None) / max(1, sum(r["viagens"] for r in rs if r["pct_via_padrao"] is not None))
    chk("Viagens que seguem a via padrão do par (ponderado)", f"{pond:.0f}%", pond >= 70)
    direta = [r for r in R if not r["troncos"]]
    chk("Viagens em rota padrão pela malha", f"{100 * K['viagens_com_tronco'] / K['viagens']:.0f}%", True,
        f"{len(direta)} pares ficam como ligação direta (curtos ou sem tronco que compense)")
    pts = sum(len(t["geo"]) for t in T)
    chk("Pontos de traçado no dashboard", pts, pts <= 120000)
    tam = os.path.getsize(os.path.join(SAIDA, "dashboard_malha.html")) / 1e6 if os.path.exists(os.path.join(SAIDA, "dashboard_malha.html")) else 0
    chk("Tamanho do dashboard (MB)", round(tam, 1), tam <= 8)
    rel = "AUDITORIA DA MALHA · " + K["periodo"] + "\n" + "\n".join(linhas)
    open(os.path.join(SAIDA, "auditoria.txt"), "w", encoding="utf-8").write(rel + "\n")
    print(rel)


if __name__ == "__main__":
    main()
