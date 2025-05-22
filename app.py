#!/usr/bin/env python3
"""
Aurora 全球数据库密码自动轮转 CDK 部署脚本
"""

import os
from aws_cdk import (
    App,
    BundlingOptions,
    CfnOutput,
    CfnParameter,
    Duration,
    RemovalPolicy,
    SecretValue,
    Stack,
)
from aws_cdk import aws_cloudwatch as cloudwatch
from aws_cdk import aws_cloudwatch_actions as cloudwatch_actions
from aws_cdk import aws_ec2 as ec2
from aws_cdk import aws_iam as iam
from aws_cdk import aws_lambda as lambda_
from aws_cdk import aws_logs as logs
from aws_cdk import aws_secretsmanager as secretsmanager
from aws_cdk import aws_sns as sns
from aws_cdk import aws_sns_subscriptions as sns_subscriptions
from constructs import Construct


class AuroraGlobalDbPasswordRotationStack(Stack):
    """Aurora 全球数据库密码自动轮转的 CDK Stack"""

    def __init__(self, scope: Construct, construct_id: str, **kwargs) -> None:
        super().__init__(scope, construct_id, **kwargs)

        # 定义 CloudFormation 参数
        secret_name_param = CfnParameter(
            self,
            "SecretName",
            type="String",
            description="Aurora 全球数据库凭证的 Secret 名称",
            default="aurora-global-db-credentials",
        )

        vpc_id_param = CfnParameter(
            self,
            "VpcId",
            type="AWS::EC2::VPC::Id",
            description="Lambda 函数将部署在此 VPC 中（必须能够访问 Aurora 数据库）",
        )

        subnet_ids_param = CfnParameter(
            self,
            "SubnetIds",
            type="List<AWS::EC2::Subnet::Id>",
            description="Lambda 函数将部署在这些子网中（必须能够访问 Aurora 数据库）",
        )

        rotation_days_param = CfnParameter(
            self,
            "RotationDays",
            type="Number",
            description="密码轮转周期（天数）",
            default=90,
            min_value=1,
            max_value=1000
        )

        notification_email_param = CfnParameter(
            self,
            "NotificationEmail",
            type="String",
            description="密码轮转失败时的通知邮箱（可选）",
            default="",
        )

        # 获取 VPC - 优先使用上下文中的值，否则使用参数值
        vpc_id = self.node.try_get_context('vpc_id')
        if vpc_id:
            # 如果在 context 中提供了 VPC ID，使用 lookup
            vpc = ec2.Vpc.from_lookup(
                self, "ImportedVpc", vpc_id=vpc_id
            )
        else:
            # 否则使用参数值创建引用
            vpc = ec2.Vpc.from_lookup(
                self, "ImportedVpc",
                vpc_id=vpc_id_param.value_as_string
            )

        # 获取私有子网 - 优先使用上下文中的值，否则使用参数值
        context_subnet_ids = self.node.try_get_context('subnet_ids')
        private_subnets = []
        if context_subnet_ids:
            # 如果在 context 中提供了子网 ID，使用这些值
            for i, subnet_id in enumerate(context_subnet_ids):
                private_subnets.append(
                    ec2.Subnet.from_subnet_id(self, f"PrivateSubnet{i}", subnet_id)
                )
        else:
            # 否则使用参数值
            for i, subnet_id in enumerate(subnet_ids_param.value_as_list):
                private_subnets.append(
                    ec2.Subnet.from_subnet_id(self, f"PrivateSubnet{i}", subnet_id)
                )

        # 创建安全组
        security_group = ec2.SecurityGroup(
            self,
            "LambdaSecurityGroup",
            vpc=vpc,
            description="Security group for Aurora password rotation Lambda",
            allow_all_outbound=True,
        )

        # 创建 Lambda 执行角色
        lambda_role = iam.Role(
            self,
            "PasswordRotationLambdaRole",
            assumed_by=iam.ServicePrincipal("lambda.amazonaws.com"),
            managed_policies=[
                iam.ManagedPolicy.from_aws_managed_policy_name(
                    "service-role/AWSLambdaBasicExecutionRole"
                ),
                iam.ManagedPolicy.from_aws_managed_policy_name(
                    "service-role/AWSLambdaVPCAccessExecutionRole"
                ),
            ],
        )

        # 添加 Secrets Manager 权限
        lambda_role.add_to_policy(
            iam.PolicyStatement(
                actions=[
                    "secretsmanager:DescribeSecret",
                    "secretsmanager:GetSecretValue",
                    "secretsmanager:PutSecretValue",
                    "secretsmanager:UpdateSecretVersionStage",
                ],
                resources=[
                    f"arn:aws:secretsmanager:{self.region}:{self.account}:secret:{secret_name_param.value_as_string}*"
                ],
            )
        )

        # 创建 Lambda 层
        pymysql_layer = lambda_.LayerVersion(
            self,
            "PyMySQLLayer",
            code=lambda_.Code.from_asset(
                "layer",
                bundling=BundlingOptions(
                    image=lambda_.Runtime.PYTHON_3_9.bundling_image,
                    command=[
                        "bash",
                        "-c",
                        "pip install -r /asset-input/requirements.txt -t /asset-output/python && cp -au /asset-input/. /asset-output",
                    ],
                ),
            ),
            compatible_runtimes=[lambda_.Runtime.PYTHON_3_9],
            description="Layer containing pymysql library",
        )

        # 创建 Lambda 函数
        rotation_lambda = lambda_.Function(
            self,
            "AuroraPasswordRotationFunction",
            runtime=lambda_.Runtime.PYTHON_3_9,
            handler="index.lambda_handler",
            code=lambda_.Code.from_asset('lambda'),
            role=lambda_role,
            timeout=Duration.seconds(60),  # 增加超时时间
            memory_size=256,
            vpc=vpc,
            security_groups=[security_group],
            vpc_subnets=ec2.SubnetSelection(subnets=private_subnets),
            layers=[pymysql_layer],
            environment={
                "SECRETS_MANAGER_ENDPOINT": f"https://secretsmanager.{self.region}.amazonaws.com",
                "MAX_RETRIES": "3",
                "RETRY_DELAY_SECONDS": "2",
            },
        )

        # 创建 CloudWatch 日志组
        logs_group = logs.LogGroup(
            self,
            "RotationLambdaLogGroup",
            log_group_name=f"/aws/lambda/{rotation_lambda.function_name}",
            retention=logs.RetentionDays.ONE_MONTH,
            removal_policy=RemovalPolicy.DESTROY,
        )

        # 创建 Secret（如果不存在）
        secret = secretsmanager.Secret(
            self,
            "AuroraGlobalDbSecret",
            secret_name=secret_name_param.value_as_string,
            description="Aurora 全球数据库凭证",
            generate_secret_string=secretsmanager.SecretStringGenerator(
                secret_string_template='{"username":"admin","host":"your-aurora-global-cluster-endpoint","port":"3306","dbname":"mysql"}',
                generate_string_key="password",
                exclude_characters='"@/\\',
            ),
        )

        # 获取轮转天数 - 优先使用上下文中的值，否则使用参数值
        rotation_days = self.node.try_get_context('rotation_days')
        if rotation_days is None:
            rotation_days = rotation_days_param.value_as_number

        # 配置密码轮转
        if rotation_days <= 0:
            raise ValueError("Rotation days must be a positive number")
        rotation_schedule = secret.add_rotation_schedule(
            "RotationSchedule",
            rotation_lambda=rotation_lambda,
            automatically_after=Duration.millis(rotation_days * 24 * 60 * 60 * 1000),
        )

        # 创建告警和通知（如果提供了邮箱）
        if notification_email_param.value_as_string:
            # 创建 SNS 主题
            topic = sns.Topic(
                self,
                "RotationFailureTopic",
                display_name="Aurora Password Rotation Failure Notifications",
            )

            # 添加邮件订阅
            topic.add_subscription(
                sns_subscriptions.EmailSubscription(
                    notification_email_param.value_as_string
                )
            )

            # 创建 Lambda 错误告警
            lambda_errors_alarm = cloudwatch.Alarm(
                self,
                "RotationLambdaErrorsAlarm",
                metric=rotation_lambda.metric_errors(),
                evaluation_periods=1,
                threshold=1,
                comparison_operator=cloudwatch.ComparisonOperator.GREATER_THAN_OR_EQUAL_TO_THRESHOLD,
                alarm_description="Aurora 密码轮转 Lambda 函数错误告警",
            )

            # 将告警操作关联到 SNS 主题
            lambda_errors_alarm.add_alarm_action(cloudwatch_actions.SnsAction(topic))

        # 输出资源信息
        CfnOutput(
            self,
            "SecretArn",
            description="Secret ARN for Aurora Global Database credentials",
            value=secret.secret_arn,
        )

        CfnOutput(
            self,
            "LambdaFunctionName",
            description="Lambda function name for password rotation",
            value=rotation_lambda.function_name,
        )

        CfnOutput(
            self,
            "SecretUpdateInstructions",
            description="Important: Please update database connection information in Secret",
            value=f"Please visit AWS Secrets Manager console to update host, username and other required information in {secret_name_param.value_as_string}",
        )


app = App()

# Ensure we have explicit environment values
account = os.environ.get('CDK_DEFAULT_ACCOUNT')
region = os.environ.get('CDK_DEFAULT_REGION')

if not account or not region:
    raise ValueError(
        "You must specify both CDK_DEFAULT_ACCOUNT and CDK_DEFAULT_REGION environment variables"
    )

AuroraGlobalDbPasswordRotationStack(app, "AuroraGlobalDbPasswordRotationStack",
                                    env={
                                        'account': account,
                                        'region': region
                                    }
                                    )
app.synth()
