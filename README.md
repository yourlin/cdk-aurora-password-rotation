# Aurora 全球数据库密码自动轮转 - CDK 部署方案

这个 AWS CDK 项目用于自动部署一个解决方案，实现 Aurora 全球数据库的主用户密码自动轮转。由于 Aurora 全球数据库不支持原生的 `ManageMasterUserPassword` 功能，此解决方案通过 AWS Secrets Manager 和 Lambda 函数实现自定义密码轮转。

## 架构概述

该解决方案包含以下组件：

- **AWS Secrets Manager Secret**：存储 Aurora 全球数据库的凭证
- **Lambda 函数**：执行密码轮转逻辑
- **Lambda 层**：包含 PyMySQL 库，用于数据库连接
- **轮转计划**：按照设定的周期（默认 90 天）自动触发密码轮转
- **VPC 配置**：确保 Lambda 函数能够访问 Aurora 数据库
- **IAM 角色和策略**：提供必要的权限
- **CloudWatch 告警**：监控密码轮转失败情况
- **SNS 通知**：在密码轮转失败时发送邮件通知

## 前提条件

- AWS 账户和适当的权限
- 已安装 AWS CLI 并配置凭证
- 已安装 Node.js (≥ 14.x) 和 npm
- 已安装 Python (≥ 3.9)，与 Lambda 运行时版本一致
- 已有 Aurora 全球数据库实例
- VPC 和子网配置，使 Lambda 函数能够访问数据库

## 安装步骤

1. **安装 AWS CDK**

```bash
npm install -g aws-cdk@2.x
```

2. **安装项目依赖**

```bash
pip install -r requirements.txt
```

项目依赖包括：
- aws-cdk-lib >= 2.0.0
- constructs >= 10.0.0
- pymysql >= 1.0.2（用于本地开发和测试）

3. **准备 Lambda 层**

Lambda 层需要包含 PyMySQL 库，项目已经包含了 `layer/requirements.txt` 文件。CDK 部署过程会自动构建这个层，但您也可以手动验证：

```bash
cd layer
pip install -r requirements.txt -t python
```

4. **引导 CDK 环境** (如果这是您第一次在此账户/区域中使用 CDK)

在引导 CDK 环境之前，您需要先在 cdk.json 中设置 VPC 相关的上下文值。这些值在 bootstrap 阶段是必需的，因为 VPC lookup 需要具体的值：

```json
{
  "context": {
    "vpc_id": "vpc-xxxxxxxx",  // 替换为您的实际 VPC ID
    "subnet_ids": ["subnet-xxxxxxxx", "subnet-yyyyyyyy"]  // 替换为您的实际子网 ID
  }
}
```

然后运行引导命令：

```bash
cdk bootstrap
```

注意：在 bootstrap 阶段，必须提供具体的 VPC ID 和子网 ID 值，因为 CDK 需要在合成阶段查找这些资源。在后续的 deploy 阶段，您可以使用参数来覆盖这些值。

5. **部署堆栈**

```bash
cdk deploy \
  --parameters VpcId=vpc-xxxxxxxx \
  --parameters SubnetIds=subnet-xxxxxxxx,subnet-yyyyyyyy \
  --parameters SecretName=aurora-global-db-credentials \
  --parameters RotationDays=90 \
  --parameters NotificationEmail=your-email@example.com
```

您可以通过参数自定义部署：

- `VpcId`: Lambda 函数将部署在此 VPC 中（必须能够访问 Aurora 数据库）
- `SubnetIds`: Lambda 函数将部署在这些子网中（必须能够访问 Aurora 数据库）
- `SecretName`: Aurora 全球数据库凭证的 Secret 名称
- `RotationDays`: 密码轮转周期（天数），默认为 90 天
- `NotificationEmail`: 密码轮转失败时的通知邮箱（可选）

## 部署后配置

部署完成后，您需要手动更新 Secret 中的数据库连接信息：

1. 登录 AWS 控制台，导航到 Secrets Manager
2. 找到名为 `aurora-global-db-credentials` 的 Secret（或您指定的名称）
3. 编辑 Secret 值，确保包含以下字段：
   - `username`: 数据库主用户名
   - `password`: 当前密码
   - `host`: Aurora 全球数据库主集群端点
   - `port`: 数据库端口（通常为 3306）
   - `dbname`: 数据库名称（可选）

## Lambda 层详细说明

本项目使用 Lambda 层来提供 PyMySQL 库，这样可以将依赖与函数代码分离：

1. **层的构建过程**：
   - 在部署过程中，CDK 会自动构建 Lambda 层
   - 构建过程使用 Docker 容器，确保与 Lambda 运行环境兼容
   - 构建命令会安装 `layer/requirements.txt` 中指定的依赖到 `/python` 目录

2. **层的结构**：
   - `/python` 目录包含 PyMySQL 库及其依赖
   - Lambda 函数可以直接导入这些库

3. **自定义层**：
   - 如需添加其他依赖，可以编辑 `layer/requirements.txt` 文件

## Lambda 函数工作原理

Lambda 函数实现了 AWS Secrets Manager 的标准四步轮转流程：

1. **createSecret**：
   - 获取当前密钥内容
   - 生成新的强密码
   - 创建新版本的密钥（AWSPENDING 阶段）

2. **setSecret**：
   - 获取当前密钥和待设置的新密钥
   - 连接到 Aurora 主集群
   - 执行 ALTER USER 命令更新密码
   - **等待 10 秒**，允许密码更改复制到全球数据库的只读集群

3. **testSecret**：
   - 使用新密码测试连接到数据库
   - 执行简单查询验证连接

4. **finishSecret**：
   - 完成轮转，标记新密钥为当前密钥（AWSCURRENT 阶段）

每个步骤都包含错误处理和重试机制，确保轮转过程的可靠性。

## 全球数据库复制延迟说明

Aurora 全球数据库使用异步复制将更改从主集群传播到辅助集群。密码更改也遵循这一复制过程：

1. **复制延迟**：
   - 密码更改需要时间从主集群复制到辅助集群
   - 代码中包含 10 秒的等待时间，允许复制完成
   - 在高负载情况下，可能需要更长的等待时间

2. **最佳实践**：
   - 在低流量时段执行密码轮转
   - 如果复制延迟较高，考虑增加等待时间（修改 Lambda 代码中的 `time.sleep(10)` 值）
   - 监控复制延迟指标，调整等待时间

## cdk.json 配置说明

`cdk.json` 文件包含 CDK 应用程序的配置选项：

- `app`: 指定 CDK 应用程序的入口点
- `watch`: 配置 `cdk watch` 命令的监视选项
- `context`: 包含 CDK 上下文值，控制各种 CDK 构造的行为

重要的上下文设置包括：
- `@aws-cdk/aws-iam:minimizePolicies`: 最小化生成的 IAM 策略
- `@aws-cdk/core:validateSnapshotRemovalPolicy`: 验证资源的删除策略
- `@aws-cdk/aws-lambda:recognizeLayerVersion`: 启用 Lambda 层版本识别

## 监控和日志

- Lambda 函数的日志存储在 CloudWatch Logs 中，保留期为一个月
- 可以配置 CloudWatch 告警监控密码轮转失败情况
- 可以配置 SNS 通知，在密码轮转失败时发送邮件通知

## 注意事项

- 确保 Lambda 函数部署在能够访问 Aurora 数据库的 VPC 和子网中
- 确保 Secret 中的数据库连接信息正确无误
- 密码轮转应在非高峰时段进行，以减少对应用程序的影响
- 应用程序应从 Secrets Manager 获取数据库凭证，而不是硬编码
- 首次部署后，您需要手动更新 Secret 中的数据库连接信息

## 故障排除详细指南

如果密码轮转失败，请按照以下步骤进行故障排除：

1. **检查 Lambda 函数日志**：
   - 导航到 CloudWatch Logs 控制台
   - 查找名为 `/aws/lambda/[函数名]` 的日志组
   - 查看最新的日志流，寻找错误消息
   - 注意轮转步骤（createSecret、setSecret、testSecret、finishSecret）和具体错误

2. **网络连接问题**：
   - 确保 Lambda 函数的 VPC 和子网配置正确
   - 验证安全组规则允许从 Lambda 函数到 Aurora 数据库的流量（通常是 3306 端口）
   - 使用 VPC 流日志检查网络连接问题

3. **数据库凭证问题**：
   - 验证 Secret 中的连接信息是否正确
   - 确保 `host` 指向主集群端点，而不是读取器端点
   - 尝试手动使用 Secret 中的凭证连接到数据库

4. **权限问题**：
   - 确保 Lambda 函数的执行角色有足够的权限
   - 验证数据库用户是否有权限更改自己的密码
   - 检查 Secrets Manager 权限是否正确配置

5. **复制延迟问题**：
   - 如果测试连接失败，可能是由于复制延迟
   - 检查 Aurora 全球数据库的复制延迟指标
   - 考虑增加等待时间（修改 Lambda 代码）

6. **手动触发轮转**：
   - 在 Secrets Manager 控制台中，可以手动触发轮转进行测试
   - 观察轮转过程中的日志，确定失败的具体步骤

7. **常见错误代码**：
   - `AccessDenied`: 权限不足
   - `ResourceNotFoundException`: 资源不存在（如日志组或 Secret）
   - `ValidationException`: 参数验证失败
   - `InvalidParameterException`: 参数无效
   - `ServiceUnavailable`: 服务不可用

## 清理资源

要删除部署的资源，请运行：

```bash
cdk destroy
```

注意：Secret 配置了 `RemovalPolicy.RETAIN`，因此不会被自动删除，以防止意外凭证丢失。如需删除 Secret，请手动执行。
