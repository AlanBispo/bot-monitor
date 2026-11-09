# Price Monitor Bot

Um bot pessoal do Telegram que acompanha preços na Amazon, no Mercado Livre e na Magalu e avisa quando vale a pena comprar. Ele roda no seu próprio computador: enquanto o PC estiver ligado e com internet, o monitoramento continua.

## Tecnologias

Feito em **Python 3.11+**, com banco **SQLite** e o **Telegram** como interface. Não usa inteligência artificial nem serviços pagos.

## O que ele faz

### Cadastrar produtos

- **Colar um link no chat:** o jeito mais rápido. O bot salva o preço atual e avisa sempre que aparecer um preço menor.
- **Definir um preço-alvo:** `/add_alvo 29,90 LINK` avisa quando o total, já com o frete, chegar a esse valor. Se o frete for desconhecido, o bot não dispara esse alerta.
- **Buscar pelo nome:** `/buscar sabão líquido OMO 3L` procura nas lojas e mostra uma lista única, do mais barato para o mais caro. Depois é só escolher um resultado pelo botão.
- **Comparar lojas:** `/anexar ID LINK` acrescenta outra oferta ao mesmo produto, e o bot passa a acompanhar todas juntas.

### Acompanhar preços

- **Verificação automática:** cada produto é consultado a cada 30 minutos, ou a cada 10 quando o preço está perto do alvo. Com `/intervalo ID MINUTOS` você define outra frequência, e `/check` verifica tudo na hora.
- **Histórico e gráfico:** `/historico ID` mostra os preços atual, mínimo e máximo, com um gráfico.
- **Planilha:** `/exportar` envia o histórico em CSV, pronto para abrir no Excel.

### Receber alertas

- **Alerta principal:** chega na hora em que o preço atinge o alvo ou cai. Cada aviso tem botões para abrir a oferta, ver o gráfico, pausar ou silenciar o produto por 7 dias.
- **Outros avisos:** produto de volta ao estoque e outra loja mais barata. Produtos com preço-alvo também avisam do menor preço em 90 dias e de queda de 15% ou mais em 7 dias.
- **Resumo diário:** às 9h, esses outros avisos chegam juntos em uma só mensagem. `/resumo` envia o resumo na hora.
- **Horário de silêncio:** das 22h às 7h, as mensagens chegam sem som.
- **Loja com problema:** se uma loja parar de responder, o bot avisa uma vez e avisa de novo quando ela voltar.

### Vigiar ofertas

`/vigiar LG | dual inverter | 9000` procura ar-condicionado dessa marca, linha e potência a cada 6 horas, no Mercado Livre (precisa das credenciais da API oficial) e na Amazon. A primeira busca define o preço de referência, e o bot avisa a cada novo menor preço. Quedas de R$ 500 ou mais tocam mesmo no horário de silêncio. `/vigias` lista as buscas salvas e `/parar_vigia ID` encerra uma delas.

### Organizar a lista

- `/lista` mostra os produtos com botões para pausar, mudar o alvo, ver o gráfico e remover. `/lista todos` inclui os pausados.
- `/pausar ID` e `/remover ID` param o monitoramento sem apagar o histórico. `/reativar ID` desfaz.
- `/alvo ID PREÇO` muda o preço-alvo.
- `/status` mostra como está cada oferta, e `/help` lista todos os comandos.

### O que ele não faz

O bot nunca inventa preço ou frete: se uma loja bloquear o acesso ou mudar a página, ele registra a falha e segue com as outras. Também não contorna CAPTCHA nem calcula frete por CEP. O "menor preço" é o menor entre as ofertas que ele conseguiu confirmar, não o menor de toda a internet.

## Como instalar

Você precisa de Python 3.11+ e de um bot criado no Telegram com o [@BotFather](https://t.me/BotFather).

1. Instale o projeto (no PowerShell, dentro da pasta dele):

   ```powershell
   py -3.12 -m venv .venv
   .\.venv\Scripts\python.exe -m pip install -e ".[dev]"
   Copy-Item .env.example .env
   ```

2. No arquivo `.env`, preencha `TELEGRAM_BOT_TOKEN` e `TELEGRAM_ALLOWED_CHAT_ID`, que é o número do seu chat. Os demais ajustes são opcionais e estão no mesmo arquivo. Nunca compartilhe o `.env`.

3. Inicie o bot:

   ```powershell
   powershell.exe -NoProfile -ExecutionPolicy Bypass -File .\scripts\start-monitor.ps1
   ```

Para o bot iniciar sozinho sempre que você entrar no Windows, rode uma vez:

```powershell
powershell.exe -NoProfile -ExecutionPolicy Bypass -File .\scripts\install-startup.ps1 -StartNow
```

Mais detalhes de operação e diagnóstico estão em [docs/OPERACAO.md](docs/OPERACAO.md).
