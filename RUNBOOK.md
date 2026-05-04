# RUNBOOK — NYC TLC Trip Record Data Pipelines

Procedimentos operacionais para preparação, execução, validação e manutenção do pipeline de dados.

---

## Sumário

- [Pré-requisitos](#pré-requisitos)
- [1. Preparação do Ambiente Local](#1-preparação-do-ambiente-local)
- [2. Provisionamento da Infraestrutura (Terraform)](#2-provisionamento-da-infraestrutura-terraform)
- [3. Configuração do CI/CD (GitHub Actions)](#3-configuração-do-cicd-github-actions)
- [4. Build e Push da Imagem Docker](#4-build-e-push-da-imagem-docker)
- [5. Execução do Pipeline — Download dos Dados](#5-execução-do-pipeline--download-dos-dados)
- [6. Execução do Pipeline — Construção da Camada Raw](#6-execução-do-pipeline--construção-da-camada-raw)
- [7. Validação e Testes](#7-validação-e-testes)
- [8. Reexecução e Reprocessamento](#8-reexecução-e-reprocessamento)
- [9. Problemas Conhecidos e Limitações](#9-problemas-conhecidos-e-limitações)
- [10. Ações de Contingência](#10-ações-de-contingência)

---

## Pré-requisitos

### Ferramentas

| Ferramenta | Versão mínima | Finalidade |
|------------|---------------|-----------|
| Python | 3.12+ | Execução dos scripts |
| pip | — | Gerenciamento de dependências Python |
| Docker | 20.10+ | Build da imagem do container |
| Docker Buildx | — | Build multi-plataforma (ARM64) |
| Terraform | >= 1.5 | Provisionamento da infraestrutura AWS |
| AWS CLI | v2 | Interação com serviços AWS |
| Git | — | Controle de versão |

### Contas e Acessos

- **Conta AWS** com permissões para criar: S3, ECR, ECS, IAM, Glue, CloudWatch
- **Repositório GitHub** com permissão para configurar secrets e GitHub Actions
- **AWS CLI configurado** com credenciais válidas (`aws configure` ou variáveis de ambiente)

### Variáveis de Ambiente (referência)

| Variável | Obrigatória | Descrição | Padrão |
|----------|-------------|-----------|--------|
| `S3_BUCKET` | Sim | Nome do bucket S3 para dados | — |
| `S3_PREFIX` | Não | Prefixo para staging no S3 | `staging` |
| `S3_STAGING_PREFIX` | Não | Prefixo de staging (build raw) | `staging` |
| `S3_RAW_PREFIX` | Não | Prefixo de saída raw | `raw` |
| `START_MONTH` | Sim (download) | Mês inicial no formato `YYYY-MM` | — |
| `END_MONTH` | Sim (download) | Mês final no formato `YYYY-MM` | — |
| `GLUE_DATABASE` | Não | Nome do database no Glue Catalog | `trip_record_data` |
| `AWS_REGION` | Não | Região AWS | `us-east-1` |
| `SKIP_QUALITY_ANALYSIS` | Não | Pular análise de qualidade | `false` |

---

## 1. Preparação do Ambiente Local

### 1.1 Clonar o repositório

```bash
git clone https://github.com/MathEiji/deng_trip_record_data.git
cd deng_trip_record_data
```

### 1.2 Criar ambiente virtual Python

```bash
python3 -m venv .venv
source .venv/bin/activate
```

### 1.3 Instalar dependências da aplicação

```bash
pip install -r app/src/requirements.txt
```

### 1.4 Instalar dependências dos notebooks (opcional)

```bash
pip install -r notebooks/requirements.txt
```

### 1.5 Verificar AWS CLI

```bash
aws sts get-caller-identity
```

Deve retornar o ARN da sua conta/role. Caso contrário, configure com `aws configure`.

---

## 2. Provisionamento da Infraestrutura (Terraform)

### 2.1 Configurar variáveis

```bash
cd infra
cp terraform.tfvars.example terraform.tfvars
```

Editar `terraform.tfvars` com os valores do seu ambiente:

```hcl
aws_region     = "us-east-1"
project_name   = "trip-record-data"
github_org     = "seu-usuario-github"
github_repo    = "deng_trip_record_data"
s3_bucket_name = "nome-unico-global-do-bucket"
start_month    = "2025-01"
end_month      = "2025-06"
glue_database  = "trip_record_data"
```

> **Importante:** o `s3_bucket_name` deve ser globalmente único na AWS.

### 2.2 Inicializar e aplicar

```bash
terraform init
terraform plan    # Revisar os recursos que serão criados
terraform apply   # Confirmar com "yes"
```

### 2.3 Anotar os outputs

```bash
terraform output
```

Outputs relevantes:

| Output | Uso |
|--------|-----|
| `ecr_repository_url` | URL do ECR para push de imagens |
| `ecs_cluster_name` | Nome do cluster ECS |
| `s3_bucket_name` | Bucket de dados |
| `github_actions_role_arn` | ARN da role para GitHub Actions |
| `security_group_id` | SG para tasks ECS |
| `subnet_ids` | Subnets para tasks ECS |

### 2.4 Recursos criados

- Bucket S3 para dados (staging + raw)
- Bucket S3 para Terraform state (com versionamento)
- ECR repository com lifecycle policy (mantém últimas 5 imagens)
- ECS Cluster + 2 Task Definitions (download e build-raw)
- Security Group (egress-only)
- IAM Roles: ECS execution, ECS task, GitHub Actions (OIDC)
- IAM Users para desenvolvedores (leandro.sousa, caio.ribeiro, joao.albino)
- Glue Data Catalog database
- CloudWatch Log Group (retenção 1 dia)

---

## 3. Configuração do CI/CD (GitHub Actions)

### 3.1 Configurar secret no GitHub

No repositório GitHub, ir em **Settings → Secrets and variables → Actions** e criar:

| Secret | Valor |
|--------|-------|
| `AWS_ROLE_ARN` | Valor do output `github_actions_role_arn` do Terraform |

### 3.2 Trigger automático

O workflow é acionado automaticamente em pushes para `main` que alteram:
- `app/**`
- `reference/**`
- `.github/workflows/deploy.yml`

### 3.3 Trigger manual (workflow_dispatch)

No GitHub, ir em **Actions → Build & Deploy → Run workflow** e configurar:

| Parâmetro | Opções | Descrição |
|-----------|--------|-----------|
| `run_task` | `none`, `download`, `build_raw` | Qual task executar após deploy |
| `start_month` | `YYYY-MM` | Mês inicial (usado pelo download) |
| `end_month` | `YYYY-MM` | Mês final (usado pelo download) |

---

## 4. Build e Push da Imagem Docker

### 4.1 Build local (para testes)

```bash
docker buildx build \
  --platform linux/arm64 \
  -t trip-record-data:local \
  -f app/Dockerfile \
  .
```

### 4.2 Push manual para ECR

```bash
# Autenticar no ECR
aws ecr get-login-password --region us-east-1 | \
  docker login --username AWS --password-stdin <ECR_REPOSITORY_URL>

# Tag e push
docker tag trip-record-data:local <ECR_REPOSITORY_URL>:latest
docker push <ECR_REPOSITORY_URL>:latest
```

> Em operação normal, o CI/CD cuida do build e push automaticamente.

---

## 5. Execução do Pipeline — Download dos Dados

### 5.1 Via GitHub Actions (recomendado)

1. Ir em **Actions → Build & Deploy → Run workflow**
2. Selecionar `run_task: download`
3. Informar `start_month` e `end_month`
4. Clicar em **Run workflow**
5. Acompanhar os logs na aba do workflow

### 5.2 Via AWS CLI (execução direta no ECS)

```bash
SUBNETS=$(aws ec2 describe-subnets \
  --filters "Name=default-for-az,Values=true" \
  --query 'Subnets[].SubnetId' --output text | tr '\t' ',')

SG_ID=$(terraform -chdir=infra output -raw security_group_id)

aws ecs run-task \
  --cluster trip-record-data \
  --task-definition trip-record-data-download \
  --launch-type FARGATE \
  --network-configuration "awsvpcConfiguration={subnets=[$SUBNETS],securityGroups=[$SG_ID],assignPublicIp=ENABLED}" \
  --overrides '{
    "containerOverrides": [{
      "name": "download-trip-data",
      "environment": [
        {"name": "START_MONTH", "value": "2025-01"},
        {"name": "END_MONTH", "value": "2025-06"}
      ]
    }]
  }'
```

### 5.3 Execução local (desenvolvimento)

```bash
python app/src/download_trip_data.py 2025-01 2025-06 --bucket meu-bucket
```

### 5.4 Verificar resultado

```bash
aws s3 ls s3://meu-bucket/staging/ --human-readable
```

Deve listar arquivos como `fhvhv_tripdata_2025-01.parquet`, `fhvhv_tripdata_2025-02.parquet`, etc.

---

## 6. Execução do Pipeline — Construção da Camada Raw

### 6.1 Via GitHub Actions (recomendado)

1. Ir em **Actions → Build & Deploy → Run workflow**
2. Selecionar `run_task: build_raw`
3. Clicar em **Run workflow**
4. Acompanhar os logs na aba do workflow

### 6.2 Via AWS CLI (execução direta no ECS)

```bash
aws ecs run-task \
  --cluster trip-record-data \
  --task-definition trip-record-data-build-raw \
  --launch-type FARGATE \
  --network-configuration "awsvpcConfiguration={subnets=[$SUBNETS],securityGroups=[$SG_ID],assignPublicIp=ENABLED}"
```

### 6.3 O que o build-raw faz

1. Descobre os arquivos de staging no S3
2. Executa análise de qualidade (nulos, distribuições, estatísticas) — pode ser pulada com `SKIP_QUALITY_ANALYSIS=true`
3. Materializa os dados com `trip_id` e `processed_date`
4. Gera 4 tabelas raw no S3 (`raw_dispatch_base`, `raw_trip_time_location`, `raw_fare_payment`, `raw_request_flags`)
5. Converte CSVs de referência em tabelas dimensão Parquet (`dim_hvfhs_license`, `dim_base`)
6. Valida contagem de linhas e consistência de joins
7. Registra todas as tabelas no AWS Glue Data Catalog

### 6.4 Verificar resultado

```bash
# Listar tabelas raw no S3
aws s3 ls s3://meu-bucket/raw/ --recursive --human-readable

# Verificar tabelas no Glue
aws glue get-tables --database-name trip_record_data --query 'TableList[].Name'
```

Saída esperada do Glue:
```json
["raw_dispatch_base", "raw_trip_time_location", "raw_fare_payment", "raw_request_flags", "dim_hvfhs_license", "dim_base"]
```

---

## 7. Validação e Testes

### 7.1 Validação automática (embutida no pipeline)

O script `build_raw_layer.py` executa automaticamente:

- **Contagem de linhas:** cada tabela raw deve ter o mesmo número de linhas que o staging
- **Consistência de joins:** join das 4 tabelas raw por `trip_id` em uma amostra de 100k linhas — todas devem casar
- **Tabelas dimensão:** verifica que cada dimensão tem > 0 linhas

O pipeline retorna exit code `1` se qualquer validação falhar.

### 7.2 Validação manual via Athena

```sql
-- Contagem por tabela
SELECT 'dispatch' AS tbl, COUNT(*) AS n FROM trip_record_data.raw_dispatch_base
UNION ALL
SELECT 'time_loc', COUNT(*) FROM trip_record_data.raw_trip_time_location
UNION ALL
SELECT 'fare', COUNT(*) FROM trip_record_data.raw_fare_payment
UNION ALL
SELECT 'flags', COUNT(*) FROM trip_record_data.raw_request_flags;

-- Consistência de join
SELECT COUNT(*)
FROM trip_record_data.raw_dispatch_base d
JOIN trip_record_data.raw_trip_time_location t ON d.trip_id = t.trip_id
JOIN trip_record_data.raw_fare_payment f ON d.trip_id = f.trip_id
JOIN trip_record_data.raw_request_flags r ON d.trip_id = r.trip_id;
```

### 7.3 Validação via DuckDB (local)

```python
import duckdb
con = duckdb.connect()

for table in ['raw_dispatch_base', 'raw_trip_time_location', 'raw_fare_payment', 'raw_request_flags']:
    n = con.execute(f"SELECT COUNT(*) FROM read_parquet('data/raw/{table}.parquet')").fetchone()[0]
    print(f"{table}: {n:,} rows")
```

### 7.4 Verificar logs do ECS

```bash
# Listar log streams recentes
aws logs describe-log-streams \
  --log-group-name /ecs/trip-record-data \
  --order-by LastEventTime \
  --descending \
  --limit 5

# Ler logs de um stream específico
aws logs get-log-events \
  --log-group-name /ecs/trip-record-data \
  --log-stream-name <stream-name>
```

---

## 8. Reexecução e Reprocessamento

### 8.1 Re-download de dados

O script de download é **idempotente**: arquivos que já existem no S3 são automaticamente pulados. Para forçar o re-download de um mês específico, delete o arquivo do S3 primeiro:

```bash
aws s3 rm s3://meu-bucket/staging/fhvhv_tripdata_2025-03.parquet
```

Depois execute o download normalmente.

### 8.2 Reprocessamento da camada raw

O build-raw **sobrescreve** as tabelas raw existentes no S3 e atualiza as definições no Glue. Para reprocessar:

1. Execute o build-raw novamente (via GitHub Actions ou CLI)
2. As tabelas serão recriadas com um novo `processed_date`
3. O Glue será atualizado automaticamente

### 8.3 Adicionar novos meses

1. Execute o download com o novo intervalo de meses
2. Execute o build-raw — ele processa **todos** os arquivos de staging encontrados no S3

### 8.4 Atualizar tabelas dimensão

1. Edite os CSVs em `reference/` (`dim_hvfhs_license.csv`, `dim_base.csv`)
2. Faça commit e push para `main`
3. O CI/CD rebuilda a imagem automaticamente
4. Execute o build-raw para regenerar as dimensões em Parquet

---

## 9. Problemas Conhecidos e Limitações

| Problema | Descrição | Mitigação |
|----------|-----------|-----------|
| **Timeout no download** | Arquivos grandes (~500 MB) podem causar timeout em conexões instáveis | O script usa streaming com chunks de 8 MB e multipart upload; re-executar pula arquivos já baixados |
| **Memória no build-raw** | DuckDB materializa todos os dados de staging em memória/disco | A task ECS usa 4 GB de RAM + 40 GB de ephemeral storage; para volumes muito grandes, considerar processar em batches |
| **Sem particionamento por data** | As tabelas raw são escritas como arquivo único por tabela | Para volumes maiores, implementar particionamento por `processed_date` ou mês |
| **Sem Spot/Savings Plans** | O pipeline usa Fargate on-demand | Aceitável para execuções esporádicas; para execuções frequentes, considerar Fargate Spot |
| **Retenção de logs curta** | CloudWatch Logs com retenção de 1 dia | Aumentar em `cloudwatch.tf` se necessário para debugging |
| **Free tier limitado** | ECS Fargate free tier válido por 12 meses | Monitorar custos após o período de free tier |
| **Dados disponíveis com atraso** | A TLC publica os dados com ~2 meses de atraso | Verificar disponibilidade antes de configurar o intervalo de meses |

---

## 10. Ações de Contingência

### Task ECS falhou

1. Verificar logs no CloudWatch:
   ```bash
   aws logs filter-log-events \
     --log-group-name /ecs/trip-record-data \
     --filter-pattern "ERROR"
   ```
2. Causas comuns:
   - **Sem conectividade:** verificar se a task tem `assignPublicIp=ENABLED` e o security group permite egress
   - **Permissão S3:** verificar a IAM role da task (`trip-record-data-ecs-task`)
   - **Arquivo não encontrado no CDN:** o mês solicitado pode não estar disponível ainda

### Terraform state corrompido

1. O bucket de state tem versionamento habilitado
2. Listar versões: `aws s3api list-object-versions --bucket <bucket>-tfstate --prefix trip-record-data/terraform.tfstate`
3. Restaurar versão anterior se necessário

### Glue tables inconsistentes

1. Re-executar o build-raw — ele faz upsert (create or update) das tabelas no Glue
2. Para limpar manualmente:
   ```bash
   aws glue delete-table --database-name trip_record_data --name <table_name>
   ```

### Imagem Docker não atualiza no ECS

1. Verificar se o CI/CD completou com sucesso
2. Verificar a imagem no ECR:
   ```bash
   aws ecr describe-images --repository-name trip-record-data --query 'imageDetails | sort_by(@, &imagePushedAt) | [-1]'
   ```
3. A task definition deve apontar para a imagem com o SHA do commit mais recente

### Custos inesperados

1. Verificar o AWS Cost Explorer
2. Principais fontes de custo: S3 storage, ECS Fargate compute, data transfer
3. Para reduzir custos:
   - Deletar dados de staging após o build-raw: `aws s3 rm s3://meu-bucket/staging/ --recursive`
   - Usar lifecycle policies no S3 para mover dados antigos para Glacier
   - Reduzir a retenção de logs (já está em 1 dia)
