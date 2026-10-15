terraform {
  required_version = ">= 1.5.0"

  required_providers {
    aws = {
      source  = "hashicorp/aws"
      version = "~> 5.0"
    }
    random = {
      source  = "hashicorp/random"
      version = "~> 3.6"
    }
  }

  # State local a proposito para el Paso 1 (ver docs/deployment/02-plan-implementacion.md
  # §0): no hay varias personas aplicando cambios todavia. No se versiona en git
  # (ver .gitignore).
  backend "local" {
    path = "terraform.tfstate"
  }
}

provider "aws" {
  region = var.aws_region

  default_tags {
    tags = local.tags
  }
}
