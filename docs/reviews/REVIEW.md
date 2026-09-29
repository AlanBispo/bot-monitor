# Revisão final do Price Monitor Bot

## Status

**APPROVED** — nenhum defeito bloqueante confirmado permanece. A implementação foi comparada com `docs/SPEC.md`, corrigida nesta execução e validada com testes e transportes determinísticos, sem chamadas a lojas ou Telegram ao vivo.

## Resumo da revisão

Foram inspecionados `AGENTS.md`, a especificação integral, as instruções de Coder e Reviewer, o relatório anterior, todos os módulos da aplicação, testes e fixtures, `pyproject.toml`, `.env.example`, `.gitignore` e `README.md`. O relatório anterior foi tratado como pista, não como prova. Os defeitos antigos de agrupamento monetário, disponibilidade e histórico já tinham correções no código fornecido e foram verificados novamente. A revisão encontrou e corrigiu falhas adicionais de sessão, variante, callback, dados estruturados, persistência de identidade e alertas.

## Validações executadas

| Comando ou ferramenta | Resultado |
|---|---|
| `.venv/bin/python -m pytest` | **PASS** — 158 testes em 5,96 s. |
| `.venv/bin/ruff check .` | **PASS** — `All checks passed!`. |
| `.venv/bin/bandit -r app main.py -q` | **PASS** — saída vazia, código 0; nenhum achado. Uma execução sem `-q` também informou zero achados e zero supressões. |
| `.venv/bin/python -m pip check` | **PASS** — `No broken requirements found.` |
| `.venv/bin/pip-audit` | **PASS com limitação** — `No known vulnerabilities found`; o pacote local não publicado `price-monitor 0.1.0` foi ignorado pela consulta ao PyPI. |
| `rg -l` para tokens com formato de Telegram e atribuições preenchidas de segredos, sem imprimir valores e excluindo o `.env` privado, `.venv`, banco e artefatos gerados | **PASS** — nenhum arquivo correspondente. |
| `rg` para `eval`, `exec`, `pickle`, subprocesso, `shell=True`, supressões e marcadores de trabalho pendente em código e testes | **PASS** — nenhuma ocorrência. |
| Verificação de `.venv/bin/mypy` e `.venv/bin/pyright` | **Não aplicável** — não há verificador de tipos configurado ou instalado no ambiente do projeto. |
| `git status --short --branch` | **Indisponível** — o diretório entregue não contém `.git`; diff e histórico não puderam ser examinados. |

As execuções iniciais da suíte e do lint foram repetidas após cada correção. As falhas intermediárias foram investigadas e corrigidas; os números acima são da validação final.

## Correções realizadas

- `app/stores/amazon.py`: disponibilidade passou a exigir evidência afirmativa inequívoca; texto negativo produz observação indisponível, e texto desconhecido falha sem criar snapshot ou alerta. Mensagens condicionais, como disponibilidade exclusiva para Prime, não são aceitas como compra garantida.
- `app/query.py` e `app/matching.py`: variantes de busca preservam todos os termos; filtros de preço máximo malformados são rejeitados; modelos de um caractere, tamanhos, unidades equivalentes, condição e cores reconhecidas são comparados de forma determinística. A pontuação lexical ignora unidades e filtros após aplicar restrições rígidas.
- `app/discovery.py` e `app/stores/structured.py`: ordenação estável das ofertas; falha de um candidato não descarta outros; JSON-LD ambíguo falha de forma segura; indisponibilidade explícita sem preço pode ser observada; tipos Schema.org em forma de URL e moeda ISO BRL são aceitos.
- `app/bot.py` e `main.py`: callbacks de seleção entram no polling; sessão expirada, oferta alterada ou variante conflitante impedem confirmação; `/anexar` compara identidade; preços condicionais aparecem separadamente; URLs canônicas são validadas; erros SQLite em comandos têm resposta segura; segredos do provedor web entram no filtro de logs.
- `app/database.py` e `app/models.py`: sessões expiram mesmo sem nova busca e obedecem ao limite de retenção; cortes de histórico usam UTC; identidade conhecida e incerteza inicial do listing são persistidas, inclusive na migração de um banco existente.
- `app/monitor.py` e `app/alerts.py`: monitoramento direto usa concorrência limitada, impede ciclos simultâneos e ignora produto ou loja divergente; o registro de alerta antecede o envio, e uma entrega que falha remove a tentativa para permitir repetição controlada.
- `.env.example` e `README.md`: documentam limites, flags de automação de navegador desativadas, DNS público para descoberta web, matching, expiração e operação local.
- Testes novos ou reforçados cobrem o cenário obrigatório, as três lojas com transporte HTTP falso, disponibilidade negativa/desconhecida, moeda e JSON-LD, matching e variantes, sessões, redirects, autorização, isolamento de falhas, migração SQLite, histórico, erro de banco e ordem de persistência dos alertas.

## Achados bloqueantes restantes

Nenhum.

## Achados não bloqueantes

- **INFO — risco de compatibilidade futura, `app/network.py:14`:** a conexão com IP validado utiliza uma classe interna de `httpcore`. Funciona e foi testada no ambiente instalado; uma versão futura da biblioteca pode alterar essa interface. Revalidar o transporte ao atualizar `httpx`/`httpcore`.

## Requisitos de `docs/SPEC.md` ainda não atendidos

Nenhum requisito central foi identificado como incompleto após as correções. A automação de navegador e a estimativa de frete por CEP permanecem desativadas conforme o escopo opcional; preços sem frete confiável continuam fora do menor preço garantido e dos alertas.

## Testes ausentes ou insuficientes

Nenhuma lacuna bloqueante conhecida. Não foram executados testes prolongados do scheduler, simulação de corrupção ou esgotamento de disco SQLite, nem chamadas reais às lojas ou ao Telegram. Esses testes externos não são usados como prova de correção nesta revisão, conforme o pedido de fixtures determinísticas.

## Avaliação de segurança

Os comandos e callbacks exigem chat privado autorizado. URLs de produto e redirects passam por validação de esquema e domínio, resolução DNS pública e conexão fixada ao endereço validado; corpos HTTP, redirecionamentos, candidatos, concorrência e timeouts são limitados. SQL usa parâmetros. Não foram encontrados execução de conteúdo externo, bypass de CAPTCHA, segredos preenchidos em código/testes/documentação ou supressões da análise estática. `.env` existe localmente e está em `.gitignore`; seu conteúdo privado não foi exibido nem usado em testes. Sem `.git`, não é possível afirmar nada sobre commits anteriores.

## Limitações conhecidas da revisão

- A compatibilidade com HTML atual das lojas, proteção anti-bot e entrega real do Telegram não foi verificada ao vivo. O comportamento foi provado com fixtures e transportes falsos.
- Não havia `.git`, então diff, histórico e estado de versionamento não estavam disponíveis.
- Não há verificador de tipos configurado; a auditoria de vulnerabilidades não examinou o próprio pacote local não publicado.
- Uma interrupção do processo entre reservar um alerta e enviá-lo pode deixar a tentativa registrada sem entrega. Sem uma confirmação transacional do Telegram, a aplicação prioriza evitar envios duplicados; falhas de envio confirmadas removem a tentativa e podem ser repetidas.
