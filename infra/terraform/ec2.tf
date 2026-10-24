resource "aws_ssm_parameter" "secret" {
  for_each = {
    "llm-api-key"         = var.llm_api_key
    "postgres-password"   = var.postgres_password
    "shop-admin-token"    = var.shop_admin_token
    "opspilot-api-secret" = var.opspilot_api_secret
  }
  name  = "/${var.project}/${each.key}"
  type  = "SecureString"
  value = each.value
}

data "aws_ssm_parameter" "al2023_ami" {
  name = "/aws/service/ami-amazon-linux-latest/al2023-ami-kernel-default-x86_64"
}

resource "aws_instance" "app" {
  ami                    = data.aws_ssm_parameter.al2023_ami.value
  instance_type          = var.instance_type
  subnet_id              = data.aws_subnets.default.ids[0]
  vpc_security_group_ids = [aws_security_group.app.id]
  iam_instance_profile   = aws_iam_instance_profile.app.name
  key_name               = var.ssh_key_name != "" ? var.ssh_key_name : null

  user_data = templatefile("${path.module}/user_data.sh.tftpl", {
    project      = var.project
    region       = var.region
    repo_url     = var.repo_url
    llm_provider = var.llm_provider
    llm_model    = var.llm_model
  })
  user_data_replace_on_change = true

  metadata_options {
    http_tokens = "required" # IMDSv2 only
  }

  root_block_device {
    volume_size = 20
    volume_type = "gp3"
    encrypted   = true
  }

  tags = { Name = "${var.project}-app" }

  depends_on = [aws_ssm_parameter.secret, aws_cloudwatch_log_group.shop, aws_cloudwatch_log_group.app]
}
