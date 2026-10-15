# Paso 1 — instancia única (ver docs/deployment/02-plan-implementacion.md §1
# y docs/deployment/03-tablero-tareas.md). Reproduce la topología de
# docker-compose.yml completa (2 ControlNodes + 4 DataNodes) en una sola
# EC2. Terraform no genera, lee ni sube certs/.env: eso lo hace el
# operador fuera de este código (ver README.md de este directorio).

locals {
  tags = {
    Project = var.project_tag
    Paso    = "01-single-instance"
  }

  # Puertos publicados hoy por docker-compose.yml: cn1, cn2, datanode-1..4.
  port_rules = [
    { from = 8000, to = 8000, description = "ControlNode cn1" },
    { from = 8010, to = 8010, description = "ControlNode cn2" },
    { from = 8001, to = 8004, description = "DataNodes 1-4" },
  ]
}

data "aws_availability_zones" "available" {
  state = "available"
}

data "aws_ami" "ubuntu" {
  most_recent = true
  owners      = ["099720109477"] # Canonical

  filter {
    name   = "name"
    values = ["ubuntu/images/hvm-ssd/ubuntu-jammy-22.04-amd64-server-*"]
  }

  filter {
    name   = "virtualization-type"
    values = ["hvm"]
  }
}

resource "random_id" "secrets_suffix" {
  byte_length = 4
}

# --- Red ---------------------------------------------------------------

resource "aws_vpc" "this" {
  cidr_block           = "10.80.0.0/24"
  enable_dns_support   = true
  enable_dns_hostnames = true

  tags = { Name = "dfsha-single-instance" }
}

resource "aws_internet_gateway" "this" {
  vpc_id = aws_vpc.this.id

  tags = { Name = "dfsha-single-instance" }
}

resource "aws_subnet" "public" {
  vpc_id                  = aws_vpc.this.id
  cidr_block              = "10.80.0.0/26"
  availability_zone       = data.aws_availability_zones.available.names[0]
  map_public_ip_on_launch = true

  tags = { Name = "dfsha-single-instance-public" }
}

resource "aws_route_table" "public" {
  vpc_id = aws_vpc.this.id

  route {
    cidr_block = "0.0.0.0/0"
    gateway_id = aws_internet_gateway.this.id
  }

  tags = { Name = "dfsha-single-instance-public" }
}

resource "aws_route_table_association" "public" {
  subnet_id      = aws_subnet.public.id
  route_table_id = aws_route_table.public.id
}

resource "aws_security_group" "instance" {
  name        = "dfsha-single-instance"
  description = "ControlNodes y DataNodes del Paso 1 (instancia unica)"
  vpc_id      = aws_vpc.this.id

  dynamic "ingress" {
    for_each = { for rule in local.port_rules : "${rule.from}-${rule.to}" => rule }

    content {
      description = ingress.value.description
      from_port   = ingress.value.from
      to_port     = ingress.value.to
      protocol    = "tcp"
      cidr_blocks = var.allowed_cidr
    }
  }

  egress {
    description = "Salida sin restriccion (Docker Hub, pip, GitHub, apt)"
    from_port   = 0
    to_port     = 0
    protocol    = "-1"
    cidr_blocks = ["0.0.0.0/0"]
  }

  tags = { Name = "dfsha-single-instance" }
}

# --- Secretos: solo el "contenedor", nunca su contenido -----------------
# scripts/setup_cluster.py sigue corriendo en la máquina del operador.
# Terraform crea el bucket y el permiso de lectura; el bundle
# (certs/+.env) lo sube el operador a mano (ver README.md).

resource "aws_s3_bucket" "secrets" {
  bucket = "dfsha-test-secrets-${random_id.secrets_suffix.hex}"

  # Deliberado: sin force_destroy. "terraform destroy" debe fallar si el
  # bundle todavía está en el bucket, para forzar una confirmación
  # explícita de que ya no se necesita (T7.1 del tablero de tareas).
  force_destroy = false

  tags = { Name = "dfsha-single-instance-secrets" }
}

resource "aws_s3_bucket_public_access_block" "secrets" {
  bucket = aws_s3_bucket.secrets.id

  block_public_acls       = true
  block_public_policy     = true
  ignore_public_acls      = true
  restrict_public_buckets = true
}

resource "aws_s3_bucket_server_side_encryption_configuration" "secrets" {
  bucket = aws_s3_bucket.secrets.id

  rule {
    apply_server_side_encryption_by_default {
      sse_algorithm = "AES256"
    }
  }
}

# --- IAM: SSM (acceso administrativo) + lectura de solo este bucket -----

data "aws_iam_policy_document" "ec2_assume" {
  statement {
    actions = ["sts:AssumeRole"]

    principals {
      type        = "Service"
      identifiers = ["ec2.amazonaws.com"]
    }
  }
}

resource "aws_iam_role" "instance" {
  name               = "dfsha-single-instance"
  assume_role_policy = data.aws_iam_policy_document.ec2_assume.json
}

resource "aws_iam_role_policy_attachment" "ssm" {
  role       = aws_iam_role.instance.name
  policy_arn = "arn:aws:iam::aws:policy/AmazonSSMManagedInstanceCore"
}

data "aws_iam_policy_document" "secrets_read" {
  statement {
    sid       = "ListBucket"
    actions   = ["s3:ListBucket"]
    resources = [aws_s3_bucket.secrets.arn]
  }

  statement {
    sid       = "ReadObjects"
    actions   = ["s3:GetObject"]
    resources = ["${aws_s3_bucket.secrets.arn}/*"]
  }
}

resource "aws_iam_role_policy" "secrets_read" {
  name   = "dfsha-single-instance-secrets-read"
  role   = aws_iam_role.instance.id
  policy = data.aws_iam_policy_document.secrets_read.json
}

resource "aws_iam_instance_profile" "instance" {
  name = "dfsha-single-instance"
  role = aws_iam_role.instance.name
}

# --- Cómputo -------------------------------------------------------------

resource "aws_instance" "this" {
  ami                    = data.aws_ami.ubuntu.id
  instance_type          = var.instance_type
  subnet_id              = aws_subnet.public.id
  vpc_security_group_ids = [aws_security_group.instance.id]
  iam_instance_profile   = aws_iam_instance_profile.instance.name

  # Sin key_name: el acceso administrativo es por SSM Session Manager,
  # no por SSH (ver docs/deployment/03-tablero-tareas.md).

  user_data = templatefile("${path.module}/user_data.sh.tpl", {
    repo_url = var.repo_url
    git_ref  = var.git_ref
  })

  tags = { Name = "dfsha-single-instance" }
}
