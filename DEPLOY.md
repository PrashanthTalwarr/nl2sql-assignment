# DEPLOY.md: Reproducible AWS deployment

The app runs as one Docker container (FastAPI + read-only SQLite database) on an **EC2** instance,
pulling its image from **ECR** through an **IAM role**, with logs shipped to **CloudWatch**.

**Live URL:** http://35.90.30.116

> **Why EC2 and not App Runner:** App Runner stopped accepting new customers on April 30, 2026,
> so the same container image is deployed on EC2. The image is platform-neutral; it would also run
> unchanged on ECS Fargate or ECS Express Mode.

## Architecture

```
Browser --HTTP:80--> EC2 (t3.small, Amazon Linux 2023)
                       └─ Docker container (port 8080)
                            ├─ FastAPI app + chat UI
                            └─ pharma.db (read-only SQLite, baked into the image)
EC2 instance role ──> ECR (pull image) + CloudWatch Logs (log group /nl2sql-pharma)
App ──HTTPS──> Anthropic API
```

## Prerequisites

- Docker Desktop, Python 3.12, and the AWS CLI configured (`aws configure`, region `us-west-2`)
- An Anthropic API key

## 1. Build the database

```bash
python schema/generate_data.py      # deterministic CSVs (SEED = 42)
python scripts/load_db.py           # loads CSVs into pharma.db, builds indexes, runs ANALYZE
python scripts/check_db.py          # verifies row counts and per-role scoping
```

## 2. Build and test the image locally

```bash
docker build -t nl2sql-pharma .
docker run --rm -p 8080:8080 --env-file .env nl2sql-pharma
# open http://localhost:8080
```

## 3. Push the image to ECR

```powershell
$ACCOUNT  = (aws sts get-caller-identity --query Account --output text)
$REGION   = "us-west-2"
$REGISTRY = "$ACCOUNT.dkr.ecr.$REGION.amazonaws.com"
$IMAGE    = "$REGISTRY/nl2sql-pharma:latest"

aws ecr create-repository --repository-name nl2sql-pharma --region $REGION
aws ecr get-login-password --region $REGION | docker login --username AWS --password-stdin $REGISTRY
docker tag nl2sql-pharma:latest $IMAGE
docker push $IMAGE
```

If a large layer fails with `broken pipe`, run `docker push` again; finished layers are skipped.

## 4. Create the instance role (IAM console)

IAM → Roles → Create role → Trusted entity: **AWS service → EC2**, with these managed policies:
- `AmazonEC2ContainerRegistryReadOnly`: pull the image from ECR
- `CloudWatchAgentServerPolicy`: write container logs to CloudWatch

Role name: `ec2-nl2sql-role`. The instance uses this role, so **no AWS keys are stored on the server**.

## 5. Launch the instance (EC2 console, us-west-2)

| Setting | Value |
|---|---|
| AMI | Amazon Linux 2023 (x86_64) |
| Instance type | t3.small (x86; not `t4g`, which is ARM) |
| Key pair | None (connect with EC2 Instance Connect) |
| Security group | HTTP (80) from anywhere; SSH (22) for Instance Connect |
| Storage | 20 GiB gp3 |
| IAM instance profile | `ec2-nl2sql-role` |

## 6. Run the container (EC2 Instance Connect terminal)

```bash
sudo dnf install -y docker
sudo systemctl enable --now docker
aws ecr get-login-password --region us-west-2 | sudo docker login --username AWS --password-stdin <ACCOUNT>.dkr.ecr.us-west-2.amazonaws.com
sudo docker pull <ACCOUNT>.dkr.ecr.us-west-2.amazonaws.com/nl2sql-pharma:latest

cat > ~/app.env << 'EOF'
LLM_PROVIDER=anthropic
ANTHROPIC_API_KEY=<your key>
ANTHROPIC_MODEL=claude-sonnet-4-6
ANSWER_ANTHROPIC_MODEL=claude-haiku-4-5-20251001
SESSION_SECRET=<random secret>
RATE_LIMIT_PER_MIN=60
ANSWER_CACHE=on
ANSWER_CACHE_TTL_S=3600
EOF
chmod 600 ~/app.env

sudo docker run -d --name nl2sql --restart unless-stopped -p 80:8080 \
  --env-file ~/app.env \
  --log-driver awslogs \
  --log-opt awslogs-region=us-west-2 \
  --log-opt awslogs-group=/nl2sql-pharma \
  --log-opt awslogs-create-group=true \
  <ACCOUNT>.dkr.ecr.us-west-2.amazonaws.com/nl2sql-pharma:latest
```

- `--restart unless-stopped`: the container restarts on crash and on instance reboot
- `chmod 600`: only the owner can read the secrets file; secrets are never in the image or the repository

## 7. Verify

```bash
curl http://localhost/api/health        # {"status":"ok"}
sudo docker ps                          # STATUS: Up ... (healthy)
sudo docker logs nl2sql --tail 20       # one JSON line per chat request
```

Then open `http://<public-ip>` in a browser.

## Deploying an update

```powershell
# laptop
docker build -t nl2sql-pharma .
docker tag nl2sql-pharma:latest $IMAGE
docker push $IMAGE
```
```bash
# EC2
aws ecr get-login-password --region us-west-2 | sudo docker login --username AWS --password-stdin <ACCOUNT>.dkr.ecr.us-west-2.amazonaws.com
sudo docker pull <ACCOUNT>.dkr.ecr.us-west-2.amazonaws.com/nl2sql-pharma:latest
sudo docker rm -f nl2sql
# re-run the docker run command from step 6
```

## Logs and monitoring

- **CloudWatch:** Logs → Log groups → `/nl2sql-pharma` (queryable with Logs Insights; see DESIGN.md)
- **On the instance:** `sudo docker logs nl2sql`

## Teardown

EC2 → terminate the instance; ECR → delete the `nl2sql-pharma` repository; CloudWatch → delete the log
group; IAM → delete `ec2-nl2sql-role`.

## Production hardening (not done for the assessment)

HTTPS via an Application Load Balancer + ACM certificate (or CloudFront); an Elastic IP or a domain name;
secrets in AWS Secrets Manager; SSH closed (Session Manager instead); infrastructure as code
(CloudFormation/Terraform); ECS or ECS Express Mode for rolling deploys and multiple instances.