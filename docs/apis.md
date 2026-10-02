# APIs — mapeamento (out/2026)

Base: `https://one.cordenonsi.com.br/api/externo/` · Auth: `Authorization: Bearer $CRD_TOKEN`
Todas respondem `{sucesso, dados, meta, request_id}`; erro vem em `erro.codigo/mensagem`. Timezone America/Sao_Paulo.

## 1. `viagem/listar_cargas.php` — catálogo de viagens (cargas)
- Paginação: `limite` (máx. **150**), `offset`; `meta.tem_mais` / `meta.proximo_offset`.
- Filtros que funcionam: `data_inicio`/`data_fim` (início da viagem), `data_criacao_inicio`/`data_criacao_fim`,
  `placa`, `codigo_rota`. Baixar por janela mensal de criação é o jeito estável (offset muda com cargas novas).
- Campos: `car_codigo`, cliente, tipo carga/operação, peso, status, veículo (placa, rastreador), motorista,
  origem/destino (cidade), `rota_planejada` (codigo_rota, descricao, km_planejado), `viagem` (data_inicio/fim,
  distância prevista/realizada), `etapas[]` (coleta/entrega/fronteira, documento=CNPJ, cidade, UF, lat/lng, datas).
- Cuidados: `origem/destino` do cabeçalho às vezes divergem das etapas → usar 1ª e última etapa.
  `distancia_realizada_km` costuma vir 0. Datas de previsão com ano inválido (ex. 6202).
  ~11% das viagens finalizadas têm início = fim (lançamento instantâneo, sem janela real de GPS).

## 2. `viagem/comparativo.php?car_codigo=N` — planejado x realizado de uma viagem
- `camada_planejada.polilinha_encoded`: polilinha Google (precisão 5) da rota planejada, seguindo a estrada.
- `camada_realizada`: janela da viagem, polilinha e `pontos_gps` (simplificados), `macros_eventos`.
- `metricas_comparativo`: km planejado x realizado, desvio, tempo, % etapas/entregas.
- 1 chamada por viagem (~1 s). Algumas cargas devolvem 404 (página HTML) ou 422 (`invalid_date_interval`: carga com início depois do fim) de forma consistente — tentar outra carga da mesma rota.

## 3. `autotrac/historico_posicoes.php` — GPS por veículo
- Obrigatório: `placa` **ou** `rastreador`, `data_inicio`, `data_fim` (dia). `formato=lista`, `limite` (até 5000), `offset`.
- `otimizar_polilinha=true` devolve `polilinha`, `pontos` simplificados e `paradas` (início/fim/duração).
- Fontes: últimas ~71 h vêm da Autotrac direto (ponto a cada ~2 min, com velocidade e hodômetro);
  antes disso vem do banco histórico (ponto a cada ~15 min ≈ 16 km em rodovia, velocidade sempre 0, sem hodômetro).
- Histórico começa em 01/08/2024 (jul/2024 e antes voltam vazios). Intervalo máximo por consulta: 31 dias. Parte das placas não tem histórico gravado (ver aba Qualidade do dashboard).

## 4. `autotrac/historico_macros.php?tipo=envio|retorno` — macros Autotrac
- Obrigatório: `placa` ou `rastreador`. `retorno` = motorista → central (início/fim de jornada, refeição,
  espera, pernoite...), com lat/lng. `envio` = central → caminhão.
- **Só há dados das últimas ~71 h** (não existe histórico gravado). Para usar macros na malha é preciso
  começar a coletar e guardar a partir de agora.
