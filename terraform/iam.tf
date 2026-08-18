data "aws_iam_policy_document" "ec2_assume_role" {
  statement {
    effect  = "Allow"
    actions = ["sts:AssumeRole"]

    principals {
      type        = "Service"
      identifiers = ["ec2.amazonaws.com"]
    }
  }
}

resource "aws_iam_role" "server" {
  name               = "${var.project_name}-${var.environment}-ec2"
  description        = "SSM core permissions for the Project Zomboid host"
  assume_role_policy = data.aws_iam_policy_document.ec2_assume_role.json
}

# AWS documents this managed policy as the core instance policy for SSM managed nodes.
resource "aws_iam_role_policy_attachment" "ssm_core" {
  role       = aws_iam_role.server.name
  policy_arn = "arn:aws:iam::aws:policy/AmazonSSMManagedInstanceCore"
}

resource "aws_iam_instance_profile" "server" {
  name = "${var.project_name}-${var.environment}"
  role = aws_iam_role.server.name
}
