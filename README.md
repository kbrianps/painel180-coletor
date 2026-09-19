# Coletor do Painel da Rede de Atendimento (Ligue 180)

Coleta periodicamente os serviços da rede de atendimento à mulher publicados no
[Painel da Rede de Atendimento](https://www.gov.br/mulheres/pt-br/ligue180/painel-da-rede-de-atendimento)
do Ministério das Mulheres, e guarda um histórico das versões que mudaram.

## Como os dados são obtidos

O painel é um relatório **Power BI** público embutido na página do gov.br. Não há raspagem de
HTML: o script conversa com a API pública do Power BI em dois passos.

1. `GET .../modelsAndExploration` devolve o modelo de dados do relatório (dataset, modelo e data
   da última atualização).
2. `POST .../querydata` executa uma consulta semântica sobre esse modelo e devolve as linhas.

A consulta é **montada pelo script** sobre as entidades `dEndereço` e `dServico`, em vez de copiar
a consulta de algum gráfico do painel. Gráficos somem ou mudam de identificador quando o painel é
redesenhado; os campos do modelo são estáveis.

A resposta vem comprimida: valores repetidos viram uma máscara de bits (`R`), nulos viram outra
(`Ø`) e textos viram índices para dicionários por coluna (`ValueDicts`). O script desfaz essa
compressão em `ler_linhas`.

## O que ele grava

Cada coleta que **muda** o conteúdo cria uma pasta `dados/<data-hora-UTC>/` com:

| Arquivo | Conteúdo |
|---|---|
| `painel_servicos.csv` | um serviço por linha, 19 campos, mais a fonte e as datas |
| `painel_bruto.json` | as mesmas linhas sem formatação, com metadados do modelo |

Quando nada muda, nada é gravado: o script compara um `sha256` das linhas com o valor em
`dados/ultima_impressao.txt` e apenas registra "sem mudança" no log. Isso importa porque o painel
é atualizado de forma esporádica — entre 12/08/2026 e 19/09/2026 os dados não mudaram uma vez.

As versões mais antigas são apagadas, mantendo apenas as `PAINEL180_MANTER` mais recentes.

### Modo repositório

Com `PAINEL180_REPO` apontando para um clone deste repositório, o script grava os dois arquivos
direto em `dados/` — sempre com o mesmo nome — e faz commit e push a cada mudança. O histórico do
Git passa a ser o histórico do painel: cada commit é uma mudança real, e o diff mostra quais
serviços entraram, saíram ou tiveram dados corrigidos. Nesse modo não há rotação de pastas, e o
número de versões guardadas deixa de ser limitado.

Na VM o acesso é feito por uma chave de implantação com escrita restrita a este repositório.

## Configuração

| Variável | Padrão | Função |
|---|---|---|
| `PAINEL180_DIR` | `~/painel180/dados` | onde gravar |
| `PAINEL180_MANTER` | `5` | quantas versões manter |
| `PAINEL180_UF` | `RJ` | sigla do estado a guardar; vazio guarda o Brasil inteiro |
| `PAINEL180_REPO` | vazio | caminho de um clone deste repositório; ativa o commit automático |

O script busca sempre o país inteiro e filtra depois, então o JSON registra quantos serviços
existiam no Brasil naquele momento, mesmo guardando só um estado.

## Salvaguardas

O script se recusa a gravar, e sai com erro, se:

- vierem menos de 100 serviços no total do Brasil (indica mudança ou falha na API);
- a coluna de UF desaparecer da resposta;
- o estado escolhido ficar sem nenhum serviço.

Em qualquer um desses casos os dados já coletados permanecem intactos.

## Instalação

```bash
mkdir -p ~/painel180 && cp coleta_painel180.py ~/painel180/
sudo cp systemd/painel180.service systemd/painel180.timer /etc/systemd/system/
sudo systemctl daemon-reload
sudo systemctl enable --now painel180.timer
```

Só precisa de Python 3 — nenhuma biblioteca externa.

Para rodar uma vez, à mão:

```bash
python3 ~/painel180/coleta_painel180.py
```

Para acompanhar:

```bash
systemctl list-timers painel180.timer
journalctl -u painel180.service -n 20
```

## Origem

Os dados equivalentes foram levantados pela primeira vez em 24/08/2026, de forma manual, apenas
para o Rio de Janeiro (180 serviços). Este script reproduz exatamente aquele resultado e amplia o
alcance: busca os 2.641 serviços do país e filtra o estado desejado.
