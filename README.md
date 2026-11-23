# Price Monitor Bot

Um bot de Telegram que acompanha preços em lojas como Amazon, Mercado Livre e Magalu. Cole um link no chat ou busque pelo nome do produto para escolher o que acompanhar e receber avisos quando o preço baixar.

## O que dá para fazer

- **Acompanhar um produto.** Cole o link para receber avisos de aumentos e quedas no preço do produto ou use `/add_alvo 29,90 LINK` para definir quanto quer pagar. Dá para acompanhar anúncios de várias lojas para o mesmo produto.
- **Encontrar ofertas.** Use `/buscar sabão líquido OMO 3L` para comparar resultados e escolher qual anúncio acompanhar. Para ar-condicionado, `/vigiar LG | dual inverter | 9000` repete a busca por marca, linha e capacidade e avisa quando encontra um novo menor preço na Amazon ou no catálogo do Mercado Livre.
- **Entender a variação de preço.** Consulte o histórico com gráfico no Telegram ou exporte os dados em CSV.

## Como funciona

O bot roda localmente em **Python**, guarda o histórico no **SQLite** e usa o **Telegram** como interface. Cada loja tem uma integração própria. Depois que você escolhe uma oferta, o monitor consulta diretamente o anúncio salvo, o que mantém as verificações periódicas leves.

A busca verifica as páginas antes de ordenar as ofertas e considera frete conhecido e descontos confirmados. O bot sinaliza quando falta o frete ou uma loja falha, e usa o total confirmado para disparar o alerta de preço-alvo. Testes com respostas simuladas cobrem a busca, o cálculo de preços e os alertas.

## Rode no seu computador

Com Python 3.12 e um bot criado pelo [@BotFather](https://t.me/BotFather), abra o PowerShell na pasta do projeto:

```powershell
py -3.12 -m venv .venv
.\.venv\Scripts\python.exe -m pip install -r requirements.lock
.\.venv\Scripts\python.exe -m pip install --no-deps -e .
if (-not (Test-Path .env)) { Copy-Item .env.example .env }
```

Preencha `TELEGRAM_BOT_TOKEN` e `TELEGRAM_ALLOWED_CHAT_ID` no `.env`. Inicie o bot com o comando abaixo e envie `/start` no Telegram.

```powershell
powershell.exe -NoProfile -ExecutionPolicy Bypass -File .\scripts\start-monitor.ps1
```

O monitoramento funciona enquanto o computador estiver ligado e conectado. Os detalhes de instalação e funcionamento estão no [guia de operação](docs/OPERACAO.md) e na [especificação técnica](docs/SPEC.md).
