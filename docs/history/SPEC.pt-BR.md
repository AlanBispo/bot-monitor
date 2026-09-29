# SPEC.md
ESSA SPEC É APENAS PARA DOCUMENTAR EM PORTUGUÊS E NÃO DEVE SER UTILIZADA

# Price Monitor Bot — Especificação Funcional e Técnica

## 1. Objetivo

Construir um bot local e pessoal para monitoramento de preços de produtos.

O sistema deve permitir:
- pesquisar produtos pelo nome;
- cadastrar produtos a partir de resultados encontrados;
- cadastrar um produto diretamente por URL quando aplicável;
- monitorar preços periodicamente;
- comparar o preço atual com um preço-alvo definido pelo usuário;
- considerar preço do produto, frete e descontos/cupons quando confiáveis;
- manter histórico de preços ao longo do tempo;
- enviar alertas diretamente ao usuário via Telegram;
- permitir consultas e comandos pelo Telegram;
- executar localmente no computador do usuário;
- permanecer simples e fácil de manter.

O projeto é para uso pessoal e local. Não deve ser desenhado como um sistema distribuído ou de alta escala.

## 2. Princípios

### 2.1 Simplicidade primeiro

Não adicionar tecnologias sem benefício concreto para este caso de uso.

Evitar inicialmente:
- microserviços;
- Redis;
- filas distribuídas;
- Kubernetes;
- PostgreSQL;
- frameworks web sem necessidade;
- painel web;
- serviços externos pagos;
- infraestrutura de nuvem;
- VPS.

### 2.2 Execução local

O sistema deve funcionar enquanto o computador do usuário estiver ligado e tiver acesso à internet.

O ambiente principal pode ser Windows com execução Python local ou via WSL, desde que a configuração final seja documentada e funcional.

Quando o computador for desligado, o monitoramento pode parar.

Ao iniciar novamente, o sistema deve poder ser iniciado manualmente e, posteriormente, automatizado.

## 3. Organização da Documentação

A documentação normativa do projeto fica em:
- `docs/SPEC.md`: fonte da verdade funcional e técnica;
- `docs/agents/CODER.md`: instruções estáveis para implementação;
- `docs/agents/REVIEWER.md`: instruções estáveis para revisão;
- `docs/reviews/REVIEW.md`: artefato gerado pela revisão mais recente.

O arquivo `AGENTS.md` na raiz pode ser usado como roteador curto para essas instruções.

A estrutura do código pode evoluir conforme necessário, mas deve permanecer simples e modular.

## 4. Arquitetura

A aplicação deve ser um monólito modular em Python.

Responsabilidades esperadas:
- interface Telegram;
- monitoramento periódico;
- descoberta de produtos;
- adapters por loja;
- regras de preço;
- regras de alerta;
- persistência SQLite;
- histórico;
- configuração;
- testes.

A estrutura física dos módulos pode variar, desde que preserve separação de responsabilidades e não introduza complexidade desnecessária.

## 5. Fluxo de Descoberta

O usuário deve poder pesquisar um produto pelo nome por meio do Telegram.

O fluxo esperado é:
1. pesquisar o produto nas lojas suportadas;
2. apresentar resultados compatíveis;
3. mostrar loja, título e preço quando disponível;
4. permitir que o usuário confirme quais resultados correspondem ao produto desejado;
5. salvar os listings confirmados;
6. definir ou confirmar o preço-alvo;
7. iniciar o monitoramento periódico desses listings.

A busca ampla não deve ser repetida em cada ciclo de monitoramento.

## 6. Busca Ampla e Monitoramento

### 6.1 Busca ampla

A busca ampla deve ocorrer principalmente:
- no cadastro inicial;
- quando o usuário solicitar explicitamente;
- em redescoberta ocasional de baixa frequência, caso esse recurso seja implementado.

Objetivos:
- encontrar ofertas novas;
- encontrar páginas novas para o mesmo produto;
- detectar novos vendedores ou lojas relevantes.

### 6.2 Monitoramento periódico

Após o cadastro, o sistema deve monitorar URLs ou listings específicos previamente confirmados.

Motivos:
- maior estabilidade;
- menor consumo;
- menor risco de bloqueio;
- menos resultados irrelevantes;
- menor tráfego;
- menor complexidade.

## 7. Lojas

Lojas iniciais desejadas:
1. Amazon;
2. Mercado Livre;
3. Magalu.

Outras lojas podem ser adicionadas posteriormente.

Cada loja deve possuir integração isolada por meio de uma fronteira de adapter ou abstração equivalente.

A aplicação não deve depender de detalhes internos de uma loja fora da integração correspondente.

## 8. Estratégia de Coleta

A implementação deve preferir a solução mais simples e confiável disponível.

Ordem preferencial:
1. API pública ou oficial adequada;
2. requisição HTTP e parsing de HTML;
3. browser automatizado apenas quando necessário.

Não usar browser automatizado para todas as lojas por padrão.

Razões:
- maior consumo de recursos;
- menor velocidade;
- maior manutenção;
- maior chance de bloqueio.

## 9. Produto e Listing

Produto e listing são entidades diferentes.

Um produto representa o item que o usuário quer acompanhar.

Um listing representa uma oferta concreta em uma loja.

Um produto pode possuir vários listings.

Cada listing:
- pertence a um produto;
- pertence a uma loja;
- possui URL própria quando aplicável;
- mantém histórico próprio;
- pode ser ativado ou desativado.

## 10. Banco de Dados

SQLite é o banco padrão.

Não migrar para outro banco sem necessidade real.

O uso deve ser apropriado para execução local e baixo volume.

WAL pode ser utilizado se trouxer benefício concreto sem adicionar complexidade indevida.

## 11. Dados Persistidos

### 11.1 Produtos

Cada produto deve armazenar, no mínimo:
- identificador;
- nome;
- preço-alvo;
- estado ativo/inativo;
- datas relevantes de criação e atualização.

### 11.2 Listings

Cada listing deve armazenar, no mínimo:
- identificador;
- produto relacionado;
- loja;
- URL ou identificador equivalente;
- título;
- vendedor quando disponível;
- estado ativo/inativo;
- datas relevantes.

### 11.3 Histórico de preços

Cada consulta válida deve criar um novo snapshot.

Cada snapshot deve conseguir representar, quando disponível:
- listing;
- preço do produto;
- frete;
- desconto confirmado;
- desconto possível;
- preço efetivo;
- preço efetivo possível;
- disponibilidade;
- vendedor;
- data e hora da captura.

### 11.4 Alertas

O sistema deve persistir informação suficiente sobre alertas enviados para evitar spam e duplicação indevida.

## 12. Histórico de Preços

O histórico é append-only.

Uma nova observação válida cria um novo snapshot.

Snapshots anteriores não devem ser sobrescritos durante atualizações normais.

O histórico deve permitir obter:
- preço atual;
- menor preço registrado;
- maior preço registrado;
- média;
- menor preço em uma janela temporal;
- histórico por loja;
- histórico consolidado do produto.

Gráficos podem ser adicionados posteriormente.

## 13. Preço Efetivo

O sistema não deve comparar somente o preço exibido do produto.

O preço efetivo deve considerar:
- preço do produto;
- frete conhecido;
- descontos confirmados.

O preço-alvo deve ser comparado preferencialmente com o preço efetivo confiável.

Valores desconhecidos não devem ser inventados.

## 14. Cupons e Descontos

Nem todo desconto encontrado deve ser tratado como garantido.

O sistema deve distinguir descontos confirmados de descontos condicionais ou incertos.

Exemplos de condicionamento:
- conta específica;
- assinatura Prime;
- uso de aplicativo;
- primeira compra;
- elegibilidade;
- cupom segmentado.

Somente descontos claramente aplicáveis devem compor o preço efetivo garantido.

Descontos condicionais podem ser apresentados como preço potencial, com indicação clara de incerteza.

O sistema não deve apresentar preço condicionado como se fosse garantido.

## 15. Frete

Sempre que possível, o frete deve entrar no cálculo.

Se o frete não puder ser determinado automaticamente:
- não inventar valor;
- registrar como desconhecido;
- deixar a incerteza explícita;
- não assumir frete grátis.

Se o frete depender de localização ou CEP e não puder ser obtido de forma confiável, isso deve ser informado.

## 16. Alertas

O alerta mínimo obrigatório é o de preço-alvo atingido.

A condição é o preço efetivo confiável ser menor ou igual ao preço-alvo.

Alertas adicionais podem existir, como:
- novo menor preço;
- queda percentual relevante;
- outra loja ficou significativamente mais barata.

Esses alertas adicionais são opcionais.

### 16.1 Prevenção de spam

O sistema não deve enviar repetidamente o mesmo alerta sem mudança relevante.

A política pode ser simples, desde que impeça spam e seja previsível.

## 17. Telegram

O Telegram é a interface principal.

O bot deve:
- receber comandos;
- responder na conversa privada;
- enviar alertas automáticos;
- restringir o controle ao usuário autorizado.

Funções desejadas:
- buscar produto;
- adicionar ou confirmar produto/listing;
- listar monitoramentos;
- forçar verificação;
- consultar histórico;
- remover ou desativar monitoramento;
- consultar status;
- obter ajuda.

Os nomes exatos dos comandos podem variar, desde que essas capacidades existam.

## 18. Segurança do Telegram

O sistema é de uso pessoal.

Deve haver uma forma simples e consistente de restringir acesso.

No mínimo:
- aceitar comandos somente do chat autorizado configurado;
- rejeitar ou ignorar outros chats de forma segura;
- manter o token do bot fora do código versionado;
- carregar secrets a partir do ambiente ou mecanismo equivalente seguro.

O token completo nunca deve aparecer em logs.

## 19. Configuração

Secrets e opções variáveis devem ser configuráveis fora do código.

A configuração deve cobrir pelo menos:
- token do bot;
- chat autorizado;
- intervalo de verificação;
- caminho do banco.

Deve existir um arquivo de exemplo sem secrets reais.

Arquivos reais de ambiente com secrets devem ficar fora do versionamento.

## 20. Scheduler

O mesmo processo pode:
- manter o bot Telegram ativo;
- executar verificações periódicas.

Não é necessário criar serviços separados para essas responsabilidades.

A frequência de monitoramento deve ser configurável.

A redescoberta ampla, se existir, deve ocorrer com frequência significativamente menor que o monitoramento direto.

## 21. Inicialização Local

O sistema deve ser preparado para execução local.

A primeira entrega funcional não depende de automação de inicialização do sistema operacional.

Posteriormente, a inicialização automática pode ser configurada por mecanismos do Windows ou WSL.

Essa automação não deve exigir mudança na lógica principal da aplicação.

## 22. Logs

Logs devem ser simples e úteis.

Devem registrar, quando relevante:
- inicialização;
- verificações;
- erros de loja;
- mudanças relevantes de preço;
- alertas enviados;
- falhas temporárias;
- falhas de parsing.

Não devem registrar:
- token do Telegram;
- secrets;
- cookies sensíveis;
- headers de autenticação;
- credenciais.

## 23. Tratamento de Erros

Falha em uma loja não deve derrubar todo o monitoramento quando as outras lojas puderem continuar.

Erros transitórios devem:
- ser registrados;
- preservar dados válidos anteriores;
- não criar preço zero ou falso;
- não disparar alertas falsos.

## 24. Validação de Preço

O sistema não deve aceitar silenciosamente como preço válido:
- zero quando for resultado de falha de parsing;
- valores negativos;
- texto vazio;
- valores claramente incompatíveis sem validação;
- preço de parcela como se fosse preço total;
- percentual de desconto como preço;
- valor de frete como preço do produto.

Parsing monetário deve ser testado.

## 25. Segurança de URLs

O sistema recebe e acessa URLs e deve tratá-las como entrada não confiável quando vierem do usuário ou de fontes externas.

Requisitos:
- aceitar apenas esquemas web suportados;
- preferir domínios de lojas suportadas;
- bloquear acesso a arquivos locais;
- impedir acesso indevido a loopback;
- impedir acesso indevido a redes privadas e link-local;
- impedir acesso a endpoints de metadata;
- considerar redirecionamentos na validação;
- não executar conteúdo retornado;
- não construir comandos de shell a partir de URL ou mensagem Telegram.

## 26. Dependências

Preferir dependências conhecidas, mantidas e necessárias.

Antes de adicionar uma nova dependência, verificar se a biblioteca padrão ou uma dependência já existente resolve adequadamente.

Evitar dependências para funções triviais.

## 27. Testes

O projeto deve possuir testes automatizados.

Cobertura funcional esperada inclui, quando aplicável:

### Preço
- produto e frete;
- produto, frete e desconto;
- desconto possível não tratado como garantido;
- frete desconhecido;
- valor inválido;
- arredondamento monetário;
- preço igual ao alvo.

### Alertas
- preço acima do alvo;
- preço igual ao alvo;
- preço abaixo do alvo;
- alerta duplicado;
- mudança relevante de preço.

### Banco
- cadastro de produto;
- cadastro de listing;
- snapshot de preço;
- consulta de histórico;
- desativação;
- persistência de alerta;
- preservação de histórico.

### Segurança
- chat não autorizado;
- URL inválida;
- esquema não permitido;
- alvo local ou privado bloqueado;
- secret não aparece em logs.

### Store adapters
Sempre que possível, parsing deve ser testado com fixtures determinísticas em vez de depender da internet.

## 28. Ferramentas de Qualidade

O projeto deve manter testes e lint configurados.

Type checking, análise estática de segurança e análise de vulnerabilidades de dependências devem ser usados quando configurados e úteis.

Falhas relevantes de segurança devem ser corrigidas antes de considerar a implementação aprovada.

## 29. Escopo do MVP

O MVP deve priorizar:
1. Telegram funcional;
2. SQLite funcional;
3. cadastro de produto;
4. preço-alvo;
5. pelo menos uma loja funcionando de ponta a ponta;
6. histórico;
7. monitoramento periódico;
8. alerta Telegram;
9. testes;
10. expansão gradual para as demais lojas.

Não tentar resolver simultaneamente todas as lojas se isso impedir uma primeira entrega funcional confiável.

## 30. Fora do Escopo Inicial

Não implementar sem solicitação posterior:
- painel web;
- aplicativo mobile;
- autenticação multiusuário;
- pagamentos;
- infraestrutura em nuvem;
- VPS;
- Kubernetes;
- microserviços;
- fila distribuída;
- banco remoto;
- IA ou LLM para identificação de produtos;
- scraping agressivo;
- bypass de CAPTCHA;
- técnicas para contornar mecanismos de proteção dos sites.

## 31. Definition of Done

Uma implementação só deve ser considerada pronta quando:
- respeita esta especificação;
- executa localmente;
- testes relevantes passam;
- lint passa;
- não contém secrets commitados;
- não possui vulnerabilidade bloqueante conhecida;
- o bot restringe controle ao usuário autorizado;
- o histórico é persistido corretamente;
- o monitoramento periódico não executa busca ampla a cada ciclo;
- alertas não geram spam indevido;
- falha em uma loja não derruba as demais sem necessidade;
- a documentação de execução está atualizada.

## 32. Regra de Decisão

Quando houver dúvida entre uma solução simples e uma solução mais sofisticada, escolher a solução simples desde que ela seja:
- correta;
- segura;
- testável;
- compatível com esta especificação;
- fácil de manter localmente.
