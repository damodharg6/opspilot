# Copy to terraform.tfvars (gitignored) and fill in.
# Generate secrets with: python -c "import secrets;print(secrets.token_urlsafe(32))"

region           = "ap-south-1"
my_ip_cidr       = "203.0.113.7/32" # curl -s https://checkip.amazonaws.com
alert_email      = "you@example.com"
budget_limit_usd = 5

llm_provider = "groq"
llm_model    = "llama-3.3-70b-versatile"

llm_api_key         = "replace-me"
postgres_password   = "replace-me"
shop_admin_token    = "replace-me"
opspilot_api_secret = "replace-me"
