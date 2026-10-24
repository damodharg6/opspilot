# Infrastructure (AWS)

One EC2 instance runs the Docker Compose stack; managed AWS services detect
problems and hand them to OpsPilot. Everything is in Terraform.

```
shop container ──awslogs──> CloudWatch Logs (/opspilot/shop)
                              └─ metric filters ─> OpsPilot/Shop metrics ─> alarms
alarm ─> SNS ─> Lambda (in VPC) ─> POST http://<private-ip>:8080/incidents ─> queue ─> worker
worker ─> MCP servers ─> CloudWatch Logs Insights / GetMetricData / deploy history
```

## Resources

| File | What it creates |
| --- | --- |
| `network.tf` | Security groups: SSH and demo ports from your IP only; API port from the Lambda SG |
| `iam.tf` | EC2 instance role (write own logs, query own logs, read metrics, read own SSM params), Lambda role |
| `ec2.tf` | SSM SecureString secrets, Amazon Linux 2023 instance (IMDSv2 only, encrypted disk) |
| `user_data.sh.tftpl` | First boot: Docker, swap, clone, `.env` from SSM, `docker compose up` |
| `cloudwatch.tf` | Log groups (7-day retention), 4 metric filters, alarms for 5xx rate, p95 latency, memory |
| `alerting.tf` | SNS topic, email subscription, alarm-forwarder Lambda, monthly budget alerts |
| `github_oidc.tf` | GitHub OIDC provider + deploy role limited to SSM Run Command on this instance |

Cost: check each service on the AWS pricing page and the Free Tier page for your
account. The budget alert (default $5) is created by this stack; set it up first if
you build anything by hand.

## Deploy

```bash
cd infra/terraform
cp example.tfvars terraform.tfvars   # fill in; this file is gitignored
terraform init
terraform plan
terraform apply
```

Then confirm the SNS email subscription, and wait ~5 minutes for first boot
(`/var/log/opspilot-bootstrap.log` on the instance; shell via the `shell` output).

Try it: run load against `shop_url`, turn on a fault with `scripts/inject.py`
(`SHOP_URL=<shop_url>`), and watch an incident appear at `api_docs_url`.

## Continuous deployment

After `apply`, set repository variables in GitHub (Settings → Variables):
`DEPLOY_ENABLED=true`, `AWS_REGION`, `AWS_DEPLOY_ROLE_ARN` (output
`github_deploy_role_arn`), `EC2_INSTANCE_ID` (output `instance_id`), and create a
`production` environment. Every green CI run on `main` then deploys that exact commit.

## Tear down

```bash
terraform destroy
```

The AWS free plan on this account ends **Jan 3, 2027**. Destroy (or upgrade the
account) before then.

## Design decisions and trade-offs

- **Single EC2 + Compose, not ECS/EKS.** Cheapest way to run 8 containers; the
  services are already stateless (API, MCP servers, workers), so moving to ECS
  Fargate later is a packaging change, not a redesign.
- **Lambda in the VPC calls the API's private IP.** The API is never exposed to the
  internet for alarms. The shared secret sits in the Lambda environment (encrypted
  at rest with the AWS-managed key); reading it from SSM instead would need an SSM
  VPC endpoint, which has an hourly cost.
- **Secrets in SSM SecureString**, fetched by the instance role at boot. They also
  live in local Terraform state, so state is gitignored. A team would use an S3
  backend with encryption and locking.
- **Deploys through SSM Run Command with GitHub OIDC.** No SSH keys in CI, no open
  port for deploys, and the role can only touch this one instance.
- **Read-only agent.** The instance role cannot change infrastructure; OpsPilot
  recommends, a human acts.
