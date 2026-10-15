output "instance_id" {
  description = "ID de la instancia (para 'aws ssm start-session --target')."
  value       = aws_instance.this.id
}

output "public_ip" {
  description = "IP elástica asociada a la instancia: host de ControlNode (cn1/cn2) y DataNodes para la CLI. Estable entre reinicios."
  value       = aws_eip.instance.public_ip
}

output "secrets_bucket" {
  description = "Bucket S3 donde subir cluster-bundle.tar.gz (certs/ + .env) tras setup_cluster.py."
  value       = aws_s3_bucket.secrets.bucket
}

output "ssm_session_command" {
  description = "Comando listo para abrir una sesión administrativa en la instancia."
  value       = "aws ssm start-session --target ${aws_instance.this.id} --region ${var.aws_region}"
}

output "dfsha_login_command" {
  description = "Comando orientativo para la CLI una vez el clúster está arriba (requiere certs/ca.crt local)."
  value       = "dfsha login <usuario> -u https://${aws_eip.instance.public_ip}:8000,https://${aws_eip.instance.public_ip}:8010 --ca-cert certs/ca.crt"
}
