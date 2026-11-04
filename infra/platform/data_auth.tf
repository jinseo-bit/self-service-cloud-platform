# Environment records (DynamoDB) and users (Cognito, D-101: User/Admin).

resource "aws_dynamodb_table" "environments" {
  name         = "${local.prefix}-environments"
  billing_mode = "PAY_PER_REQUEST"
  hash_key     = "env_id"

  attribute {
    name = "env_id"
    type = "S"
  }

  attribute {
    name = "owner_sub"
    type = "S"
  }

  global_secondary_index {
    name            = "by-owner"
    projection_type = "ALL"
    key_schema {
      attribute_name = "owner_sub"
      key_type       = "HASH"
    }
  }

  point_in_time_recovery {
    enabled = true
  }

  server_side_encryption {
    enabled = true
  }

  deletion_protection_enabled = true
}

resource "aws_cognito_user_pool" "users" {
  name                     = "${local.prefix}-users"
  username_attributes      = ["email"]
  auto_verified_attributes = ["email"]
  deletion_protection      = "ACTIVE"

  admin_create_user_config {
    allow_admin_create_user_only = true
  }

  password_policy {
    minimum_length                   = 12
    require_lowercase                = true
    require_numbers                  = true
    require_symbols                  = false
    require_uppercase                = true
    temporary_password_validity_days = 7
  }

  account_recovery_setting {
    recovery_mechanism {
      name     = "verified_email"
      priority = 1
    }
  }
}

resource "aws_cognito_user_pool_client" "web" {
  name                          = "${local.prefix}-web"
  user_pool_id                  = aws_cognito_user_pool.users.id
  generate_secret               = false
  explicit_auth_flows           = ["ALLOW_USER_PASSWORD_AUTH", "ALLOW_REFRESH_TOKEN_AUTH"]
  prevent_user_existence_errors = "ENABLED"
  access_token_validity         = 1
  id_token_validity             = 1
  refresh_token_validity        = 7
  token_validity_units {
    access_token  = "hours"
    id_token      = "hours"
    refresh_token = "days"
  }
}

# D-181: the Claude Code agent (MCP server) signs in with its own client. The API recognises this client id and lets it
# draft and read only; approve/delete/retry/redeploy stay with a person in the web UI. Shorter refresh token than the UI.
resource "aws_cognito_user_pool_client" "agent" {
  name                          = "${local.prefix}-agent"
  user_pool_id                  = aws_cognito_user_pool.users.id
  generate_secret               = false
  explicit_auth_flows           = ["ALLOW_USER_PASSWORD_AUTH", "ALLOW_REFRESH_TOKEN_AUTH"]
  prevent_user_existence_errors = "ENABLED"
  access_token_validity         = 1
  id_token_validity             = 1
  refresh_token_validity        = 1
  token_validity_units {
    access_token  = "hours"
    id_token      = "hours"
    refresh_token = "days"
  }
}

resource "aws_cognito_user_group" "admin" {
  name         = "admin"
  user_pool_id = aws_cognito_user_pool.users.id
  description  = "Coach role: approves PROD and budget exceptions, sees all environments"
}

resource "aws_cognito_user" "admin" {
  user_pool_id = aws_cognito_user_pool.users.id
  username     = var.admin_email
  attributes = {
    email          = var.admin_email
    email_verified = "true"
  }
}

resource "aws_cognito_user_in_group" "admin" {
  user_pool_id = aws_cognito_user_pool.users.id
  group_name   = aws_cognito_user_group.admin.name
  username     = aws_cognito_user.admin.username
}
