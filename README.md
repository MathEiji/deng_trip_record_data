<!-- Projeto de pipelines de dados para NYC TLC Trip Records -->

# NYC TLC Trip Record Data Pipelines

**Disciplina:** Engenharia de Dados — USP PECE Poli  
**Grupo:**
- Leandro Sousa
- Caio Ribeiro
- João Albino

**Repositório:** [github.com — deng_trip_record_data](https://github.com/MathEiji/deng_trip_record_data)

---

## Sumário

- [Descrição do Problema e Objetivos](#descrição-do-problema-e-objetivos)
- [Arquitetura e Tecnologias](#arquitetura-e-tecnologias)
- [Fonte de Dados](#fonte-de-dados)
- [Organização do Repositório](#organização-do-repositório)
- [Modelo de Dados — Camada Raw](#modelo-de-dados--camada-raw)
- [Instalação e Configuração](#instalação-e-configuração)
- [Execução da Aplicação](#execução-da-aplicação)
- [Infraestrutura AWS (Terraform)](#infraestrutura-aws-terraform)
- [CI/CD (GitHub Actions)](#cicd-github-actions)
- [Notebooks de Exploração](#notebooks-de-exploração)
- [Acesso aos Dados](#acesso-aos-dados)
- [Integrantes do Grupo](#integrantes-do-grupo)

---

## Descrição do Problema e Objetivos

A cidade de Nova York publica mensalmente dados abertos sobre todas as viagens realizadas por veículos de aluguel de alta demanda (FHVHV — High Volume For-Hire Vehicles), incluindo Uber, Lyft, Via e Juno. Cada arquivo mensal contém dezenas de milhões de registros (~21M linhas/mês) em formato Parquet, com informações sobre despacho, horários, localizações, tarifas e flags de solicitação.

**O problema:** os dados brutos são disponibilizados em um schema monolítico que mistura contextos distintos (despacho, tempo/localização, pagamento, flags), dificultando a análise e a governança.

**Objetivos do projeto:**
1. Construir um pipeline automatizado de **ingestão** dos dados do CDN público da TLC para o Amazon S3.
2. Implementar uma camada **raw** que normaliza o schema monolítico em tabelas contextuais, adicionando chaves de junção (`trip_id`) e coluna de partição (`processed_date`).
3. Executar **análise de qualidade** dos dados (nulos, distribuições, estatísticas descritivas) como parte do pipeline.
4. Registrar as tabelas no **AWS Glue Data Catalog** para consulta imediata via Athena, Spark ou Redshift Spectrum.
5. Automatizar todo o ciclo com **CI/CD** (GitHub Actions) e **infraestrutura como código** (Terraform), otimizado para o free tier da AWS.

---

## Arquitetura e Tecnologias

```
┌──────────────┐     ┌──────────────────┐     ┌──────────────────┐     ┌─────────────────┐
│  NYC TLC CDN │────▶│  ECS Fargate     │────▶│  Amazon S3       │────▶│  AWS Glue       │
│  (Parquet)   │     │  download task   │     │  staging/        │     │  Data Catalog   │
└──────────────┘     └──────────────────┘     └────────┬─────────┘     └────────┬────────┘
                                                       │                        │
                                                       ▼                        ▼
                                              ┌──────────────────┐     ┌─────────────────┐
                                              │  ECS Fargate     │     │  Athena /       │
                                              │  build-raw task  │     │  Spark /        │
                                              │  (DuckDB)        │     │  Redshift       │
                                              └──────────────────┘     └─────────────────┘
```

| Tecnologia | Uso |
|------------|-----|
| **Python 3.12** | Linguagem dos scripts de pipeline |
| **DuckDB** | Motor analítico para transformação e qualidade dos dados (leitura/escrita S3 via httpfs) |
| **boto3** | SDK AWS para interação com S3 e Glue |
| **Docker** | Containerização da aplicação (imagem ARM64) |
| **ECS Fargate (Graviton)** | Execução serverless dos containers na AWS |
| **Amazon S3** | Armazenamento dos dados (staging e raw) |
| **Amazon ECR** | Registry das imagens Docker |
| **AWS Glue Data Catalog** | Catálogo de metadados das tabelas raw |
| **CloudWatch Logs** | Logs dos containers |
| **Terraform** | Infraestrutura como código |
| **GitHub Actions** | CI/CD — build, deploy e execução de tasks |

---

## Fonte de Dados

- **Origem:** [NYC TLC Trip Record Data](https://www.nyc.gov/site/tlc/about/tlc-trip-record-data.page)
- **Dataset:** High Volume For-Hire Vehicle (FHVHV) Trip Records
- **Formato:** Apache Parquet (`fhvhv_tripdata_YYYY-MM.parquet`)
- **Volume típico:** ~21 milhões de linhas por mês
- **Empresas cobertas:** Uber, Lyft, Via, Juno

---

## Organização do Repositório

```
deng_trip_record_data/
├── .github/
│   └── workflows/
│       └── deploy.yml                # CI/CD: build → ECR → ECS task definition
├── app/
│   ├── Dockerfile                    # Imagem Docker para os jobs
│   └── src/
│       ├── download_trip_data.py     # Ingestão: download do CDN → S3
│       ├── build_raw_layer.py        # Transformação: staging → raw tables + Glue
│       └── requirements.txt          # Dependências da aplicação
├── infra/                            # Terraform — toda a infraestrutura AWS
│   ├── main.tf                       # Provider, backend, data sources
│   ├── variables.tf                  # Variáveis do projeto
│   ├── outputs.tf                    # Outputs (URLs, ARNs, IDs)
│   ├── backend.tf                    # Bucket S3 para Terraform state
│   ├── ecr.tf                        # Elastic Container Registry
│   ├── ecs.tf                        # ECS Cluster + Task Definitions
│   ├── iam.tf                        # Roles (ECS, GitHub Actions OIDC)
│   ├── iam_users.tf                  # Usuários IAM dos desenvolvedores
│   ├── s3.tf                         # Bucket S3 para dados
│   ├── glue.tf                       # Glue Data Catalog database
│   ├── cloudwatch.tf                 # Log group do ECS
│   └── terraform.tfvars.example      # Exemplo de variáveis (copiar e editar)
├── data/
│   ├── staging/                      # Parquets baixados (git-ignored)
│   └── raw/                          # Tabelas raw geradas (git-ignored)
├── reference/                        # Tabelas dimensão (versionadas)
│   ├── dim_hvfhs_license.csv         # Licença HVFHS → empresa
│   └── dim_base.csv                  # Base TLC → empresa
├── notebooks/
│   ├── data_check.ipynb              # Checagens ad-hoc com DuckDB + pandas
│   ├── raw_tables_exploration.ipynb  # Design e exploração das tabelas raw
│   └── requirements.txt              # Dependências dos notebooks
├── RUNBOOK.md                        # Procedimentos operacionais
├── README.md                         # Este arquivo
└── .gitignore
```

---

## Modelo de Dados — Camada Raw

O pipeline quebra o schema monolítico FHVHV em 4 tabelas fato + 2 tabelas dimensão. Todas as tabelas fato compartilham as colunas `trip_id` (chave de junção) e `processed_date` (partição no formato `yyyyMMdd`).

### Tabelas fato

| Tabela | Contexto | Colunas principais |
|--------|----------|--------------------|
| `raw_dispatch_base` | Despacho / base | `hvfhs_license_num`, `dispatching_base_num`, `originating_base_num` |
| `raw_trip_time_location` | Tempo e localização | `request_datetime`, `on_scene_datetime`, `pickup_datetime`, `dropoff_datetime`, `PULocationID`, `DOLocationID`, `trip_miles`, `trip_time` |
| `raw_fare_payment` | Tarifa e pagamento | `base_passenger_fare`, `tolls`, `bcf`, `sales_tax`, `congestion_surcharge`, `airport_fee`, `tips`, `driver_pay`, `cbd_congestion_fee` |
| `raw_request_flags` | Flags de solicitação | `shared_request_flag`, `shared_match_flag`, `access_a_ride_flag`, `wav_request_flag`, `wav_match_flag` |

### Tabelas dimensão

| Tabela | Finalidade | Colunas |
|--------|-----------|---------|
| `dim_hvfhs_license` | Licença HVFHS → empresa | `hvfhs_license_num`, `company_name`, `dispatching_base_num`, `status` |
| `dim_base` | Base TLC → empresa | `base_number`, `base_name`, `parent_company`, `base_type` |

---

## Instalação e Configuração

### Pré-requisitos

- Python 3.12+
- Docker (para build da imagem)
- Terraform >= 1.5 (para infraestrutura AWS)
- Conta AWS com permissões adequadas
- AWS CLI configurado (`aws configure`)

### Dependências Python

```bash
# Aplicação (download + build raw)
pip install -r app/src/requirements.txt

# Notebooks (exploração local)
pip install -r notebooks/requirements.txt
```

### Configuração da infraestrutura

```bash
cd infra
cp terraform.tfvars.example terraform.tfvars
# Editar terraform.tfvars com seus valores:
#   aws_region, github_org, github_repo, s3_bucket_name, etc.
terraform init
terraform plan
terraform apply
```

Após o `terraform apply`, configure o secret no GitHub:

| Secret | Valor (output do Terraform) |
|--------|-----------------------------|
| `AWS_ROLE_ARN` | `github_actions_role_arn` |

> Para instruções detalhadas passo a passo, consulte o [RUNBOOK.md](RUNBOOK.md).

---

## Execução da Aplicação

### Download dos dados (ingestão)

```bash
# Local (requer AWS CLI configurado)
python app/src/download_trip_data.py 2025-01 2025-06 --bucket meu-bucket

# Via ECS Fargate (workflow_dispatch no GitHub Actions)
# Selecionar run_task=download, informar start_month e end_month
```

### Construção da camada raw

```bash
# Via ECS Fargate (workflow_dispatch no GitHub Actions)
# Selecionar run_task=build_raw

# Ou localmente com variáveis de ambiente:
export S3_BUCKET=meu-bucket
export AWS_REGION=us-east-1
python app/src/build_raw_layer.py
```

> Para o passo a passo completo de execução e validação, consulte o [RUNBOOK.md](RUNBOOK.md).

---

## Infraestrutura AWS (Terraform)

Todos os recursos são definidos em `infra/` e otimizados para o free tier da AWS:

| Recurso | Finalidade | Free tier |
|---------|-----------|-----------|
| **ECR** | Registry de imagens Docker | 500 MB de armazenamento |
| **ECS Fargate** (ARM64/Graviton) | Execução dos containers | 50 vCPU-hrs + 100 GB-hrs/mês (12 meses) |
| **S3** | Armazenamento de dados | 5 GB standard |
| **Glue Data Catalog** | Catálogo de tabelas | 1M objetos gratuitos |
| **CloudWatch Logs** | Logs (retenção 1 dia) | 5 GB de ingestão |

O Terraform state é armazenado em um bucket S3 separado com versionamento habilitado.

---

## CI/CD (GitHub Actions)

O workflow `.github/workflows/deploy.yml` é acionado em pushes para `main` que alteram `app/`, `reference/` ou o próprio workflow:

1. **build-and-push** — Builda a imagem Docker ARM64 e publica no ECR
2. **deploy** — Registra novas revisões das task definitions do ECS
3. **run-task** *(manual via workflow_dispatch)* — Executa a task de download ou build-raw no Fargate com parâmetros configuráveis

A autenticação com a AWS usa **OIDC** (OpenID Connect), sem credenciais estáticas armazenadas no GitHub.

---

## Notebooks de Exploração

| Notebook | Descrição |
|----------|-----------|
| `notebooks/data_check.ipynb` | Checagens ad-hoc: contagens, amostragem, validações rápidas com DuckDB e pandas |
| `notebooks/raw_tables_exploration.ipynb` | Design das tabelas raw: inspeção de schema, análise de contexto, código para escrita em Parquet |

```bash
pip install -r notebooks/requirements.txt
jupyter notebook notebooks/
```

---

## Acesso aos Dados

### Via Athena (após registro no Glue)

Após a execução do pipeline `build_raw_layer.py`, as tabelas ficam disponíveis no Glue Data Catalog (database `trip_record_data`) e podem ser consultadas diretamente no Amazon Athena:

```sql
SELECT d.hvfhs_license_num, COUNT(*) AS total_trips
FROM trip_record_data.raw_dispatch_base d
GROUP BY d.hvfhs_license_num
ORDER BY total_trips DESC;
```

### Via DuckDB (local)

```python
import duckdb
con = duckdb.connect()
con.execute("SELECT * FROM read_parquet('data/raw/raw_fare_payment.parquet') LIMIT 10").fetchdf()
```

---

## Integrantes do Grupo

| Nome | Usuário IAM |
|------|-------------|
| Leandro Sousa | `leandro.sousa` |
| Caio Ribeiro | `caio.ribeiro` |
| João Albino | `joao.albino` |
