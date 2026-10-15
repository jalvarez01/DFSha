variable "aws_region" {
  description = "Region de AWS donde se crea el entorno de pruebas."
  type        = string
  default     = "us-east-1"
}

variable "allowed_cidr" {
  description = <<-EOT
    CIDR(s) desde los que se permite conectar a los puertos del clúster
    (8000, 8010, 8001-8004). Sin default a propósito: el operador debe
    fijar su propia IP (p. ej. "203.0.113.4/32"), nunca 0.0.0.0/0 — el
    clúster expone TLS mutuo con credenciales reales de por medio.
  EOT
  type        = list(string)

  validation {
    condition     = !contains(var.allowed_cidr, "0.0.0.0/0")
    error_message = "No se permite 0.0.0.0/0 en allowed_cidr. Usa la IP publica del operador en formato CIDR."
  }
}

variable "instance_type" {
  description = "Tipo de instancia EC2 para el nodo único del Paso 1."
  type        = string
  default     = "t3.medium"
}

variable "repo_url" {
  description = "URL HTTPS pública del repositorio DFSha (sin credenciales: el repo es público)."
  type        = string
  default     = "https://github.com/jalvarez01/DFSha.git"
}

variable "git_ref" {
  description = "Rama, tag o commit de DFSha a clonar en la instancia."
  type        = string
  default     = "dfee758"
}

variable "project_tag" {
  description = "Valor del tag Project en todos los recursos, para identificarlos y poder limpiarlos."
  type        = string
  default     = "dfsha-test"
}
