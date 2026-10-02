# Inteligência de Malha — Cordenonsi

Mapeia as **rotas tronco** da operação (trechos de estrada compartilhados por várias origens/destinos) a partir
do GPS realizado (Autotrac) e da rota planejada, e propõe a padronização **ponta de origem → troncos → ponta de destino**
para substituir o cadastro de rotas CNPJ → CNPJ.

## Como rodar

```bash
pip install h3 openpyxl shapely
echo "CRD_TOKEN=<token da API>" > .env          # nunca versionar

python -m malha.coleta cargas 2022-01 2026-10   # cadastro de viagens (retomável)
python -m malha.coleta gps    2026-04 2026-09   # GPS histórico por placa x mês
python -m malha.coleta rotas  2026-04 2026-09   # traçado planejado de cada rota usada
python -m malha.coleta autotrac 2026-04 2026-09  # placas sem GPS: Autotrac ou outro rastreador
python -m malha.processa      2026-04 2026-09   # troncos, nós, pontas, rotas padrão
python -m malha.dashboard                       # gera data/saida/dashboard_malha.html (+ versão para publicar)
python -m malha.auditoria                       # checa a qualidade da malha gerada (OK / ATENÇÃO)
```

O processamento consulta o roteirizador OSRM (OpenStreetMap) para o traçado e o km real dos troncos e pontas;
as respostas ficam em cache em `data/osrm/`. Para usar um servidor OSRM próprio: `MALHA_OSRM=https://...`.

Saídas em `data/saida/` (fora do git, contém dados confidenciais):
- `dashboard_malha.html` — dashboard único (abre no navegador; mapa usa OpenStreetMap)
- `malha.xlsx` — mesmas tabelas em Excel (Rotas padrão, Troncos, Nós, Pontas, Placas sem GPS)

## Como funciona (resumo)

1. Cada viagem vira uma trilha em hexágonos H3 (~36 km²): GPS realizado quando liga origem ao destino;
   senão, o traçado da rota planejada.
2. Hexágono é **tronco** quando passam ≥ 5 pares cidade→cidade distintos e ≥ 5 viagens/mês.
3. O grafo é quebrado em trechos entre nós (bifurcações e pontos com ≥ 4 entradas/saídas por mês).
   Nós a menos de 20 km viram um **polo** (no máximo 30 km de diâmetro); trechos internos ao polo e trechos
   com < 2 viagens/mês saem da malha.
4. Trechos viram **corredores (troncos)** quando o fluxo atravessa o polo em linha reta; corredores que serpenteiam
   são quebrados, e corredores vizinhos da mesma rodovia seguindo reto são unidos.
5. Cada tronco é roteado por estrada (OSRM): traçado real, km real e rodovias (BR-116, BR-101...).
6. **Rota padrão** de cada par cidade→cidade = menor custo pela malha (ponta × 1,7 + tronco + 25 km por troca de tronco,
   com 15% de bônus nos troncos que o par já usa). Se ficar > 1,25 × a ligação direta, o par fica como **ligação direta**.
   O planejador do dashboard usa exatamente a mesma regra.
7. Nomes: municípios do IBGE no Brasil; Natural Earth no exterior.

## Mapa-base

- Limites de UFs e municípios: malha IBGE (`python -m malha.ref_malhas` regenera `malha/ref/basemap.json`).
- Rodovias federais (BR), estaduais (SC-, SP-, SPA-, ERS-...) e rotas nacionais de AR/CL/PY: OpenStreetMap
  (`python -m malha.rodovias` regenera `malha/ref/rodovias.json` a partir dos extratos em `data/osm/`,
  baixados de https://download.openstreetmap.fr/extracts/south-america/). Dados © colaboradores do OpenStreetMap (ODbL).

Parâmetros no topo de `malha/processa.py`. Detalhes das APIs: [docs/apis.md](docs/apis.md).
