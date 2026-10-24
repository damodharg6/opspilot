output "instance_id" {
  value = aws_instance.app.id
}

output "public_ip" {
  value = aws_instance.app.public_ip
}

output "shop_url" {
  value = "http://${aws_instance.app.public_ip}:8000"
}

output "api_docs_url" {
  value = "http://${aws_instance.app.public_ip}:8080/docs"
}

output "shell" {
  description = "Open a shell without SSH"
  value       = "aws ssm start-session --target ${aws_instance.app.id} --region ${var.region}"
}
