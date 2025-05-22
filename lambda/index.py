import boto3
import pymysql
import os
import json
import logging
import string
import random
import time
from botocore.exceptions import ClientError

logger = logging.getLogger()
logger.setLevel(logging.INFO)

PASSWORD_LENGTH = 32

MAX_RETRIES = int(os.environ.get('MAX_RETRIES', '3'))
RETRY_DELAY_SECONDS = int(os.environ.get('RETRY_DELAY_SECONDS', '2'))

def lambda_handler(event, context):
    logger.info("开始处理密码轮转请求")
    
    arn = event['SecretId']
    token = event.get('ClientRequestToken', None)
    step = event.get('Step', 'createSecret')
    
    logger.info(f"当前执行步骤: {step}")
    logger.info("密码轮转流程包含四个步骤:")
    logger.info("1. createSecret: 创建新的密钥版本 (AWSPENDING)")
    logger.info("2. setSecret: 使用新密码更新数据库")
    logger.info("3. testSecret: 测试新密码是否可用")
    logger.info("4. finishSecret: 完成轮转，更新 AWSCURRENT 版本")
    
    endpoint_url = os.environ.get('SECRETS_MANAGER_ENDPOINT')
    client = boto3.client('secretsmanager', endpoint_url=endpoint_url) if endpoint_url else boto3.client('secretsmanager')
    
    try:
        if step == 'createSecret':
            current_secret = get_secret_dict(client, arn, "AWSCURRENT")
            
            new_password = generate_password()
            
            create_new_secret_version(client, arn, token, current_secret, new_password)
            logger.info(f"已为密钥 {arn} 创建新的密钥版本")
            
        elif step == 'setSecret':
            current_secret = get_secret_dict(client, arn, "AWSCURRENT")
            pending_secret = get_secret_dict(client, arn, "AWSPENDING", token)
            
            if not pending_secret:
                raise ValueError("无法获取 AWSPENDING 密钥版本")
                
            update_db_password_with_retry(current_secret, pending_secret)
            logger.info(f"已更新数据库 {current_secret['host']} 的主用户密码")
            
        elif step == 'testSecret':
            pending_secret = get_secret_dict(client, arn, "AWSPENDING", token)
            
            if not pending_secret:
                raise ValueError("无法获取 AWSPENDING 密钥版本")
                
            test_db_connection_with_retry(pending_secret)
            logger.info(f"已成功测试新密码连接到数据库 {pending_secret['host']}")
            
        elif step == 'finishSecret':
            # 在完成步骤中，确保新密码已更新到 Secrets Manager
            pending_secret = get_secret_dict(client, arn, "AWSPENDING", token)
            if not pending_secret:
                raise ValueError("无法获取 AWSPENDING 密钥版本")
            
            # 验证数据库密码是否已更新
            test_db_connection_with_retry(pending_secret)
            
            # 更新 AWSCURRENT 版本的密钥
            current_secret = get_secret_dict(client, arn, "AWSCURRENT")
            current_secret['password'] = pending_secret['password']
            
            client.put_secret_value(
                SecretId=arn,
                SecretString=json.dumps(current_secret),
                VersionStages=['AWSCURRENT']
            )
            
            logger.info(f"密钥 {arn} 轮转完成，新密码已更新到 Secrets Manager")
        
        else:
            logger.error(f"未知的轮转步骤: {step}")
            raise ValueError(f"未知的轮转步骤: {step}")
            
        return f"密码轮转步骤 {step} 完成"
        
    except Exception as e:
        logger.error(f"密码轮转失败: {str(e)}")
        raise

def get_secret_dict(client, arn, stage, token=None):
    try:
        kwargs = {'SecretId': arn, 'VersionStage': stage}
        if token:
            kwargs['VersionId'] = token
            
        response = client.get_secret_value(**kwargs)
        secret_string = response['SecretString']
        secret_dict = json.loads(secret_string)
        
        required_fields = ['username', 'password', 'host', 'port', 'dbname']
        for field in required_fields:
            if field not in secret_dict:
                if field == 'port':
                    secret_dict['port'] = '3306'
                elif field == 'dbname':
                    secret_dict['dbname'] = 'mysql'
                else:
                    raise KeyError(f"密钥缺少必要字段: {field}")
                    
        return secret_dict
        
    except ClientError as e:
        if e.response['Error']['Code'] == 'ResourceNotFoundException':
            if stage == "AWSPENDING":
                logger.info(f"AWSPENDING 密钥版本不存在, 可能是首次轮转")
                return None
        logger.error(f"获取密钥失败: {str(e)}")
        raise

def generate_password():
    lowercase = string.ascii_lowercase
    uppercase = string.ascii_uppercase
    digits = string.digits
    special = "!@#$%^&*()_+-=[]{}|;:,.<>?"
    
    pwd = [
        random.choice(lowercase),
        random.choice(uppercase),
        random.choice(digits),
        random.choice(special)
    ]
    
    remaining_length = PASSWORD_LENGTH - len(pwd)
    all_chars = lowercase + uppercase + digits + special
    pwd.extend(random.choice(all_chars) for _ in range(remaining_length))
    
    random.shuffle(pwd)
    
    return ''.join(pwd)

def create_new_secret_version(client, arn, token, current_secret, new_password):
    new_secret = current_secret.copy()
    new_secret['password'] = new_password
    
    retry_count = 0
    last_exception = None
    current_token = token
    
    logger.info(f"开始为密钥 {arn} 创建新版本 (AWSPENDING)")
    
    while retry_count < MAX_RETRIES:
        try:
            client.put_secret_value(
                SecretId=arn,
                ClientRequestToken=current_token,
                SecretString=json.dumps(new_secret),
                VersionStages=['AWSPENDING']
            )
            logger.info(f"成功创建新密钥版本 (AWSPENDING)，等待 Secrets Manager 触发下一步 'setSecret'")
            logger.info("轮转流程将按以下顺序继续：1. setSecret (更新数据库密码) -> 2. testSecret (测试新密码) -> 3. finishSecret (完成轮转)")
            return
        except client.exceptions.ResourceExistsException as e:
            last_exception = e
            retry_count += 1
            logger.warning(f"Token {current_token} 已被使用，尝试使用新token重试 ({retry_count}/{MAX_RETRIES})")
            if retry_count < MAX_RETRIES:
                # Generate a new token for retry
                current_token = f"{token}-retry-{retry_count}"
                time.sleep(RETRY_DELAY_SECONDS)
                continue
            break
        except Exception as e:
            logger.error(f"创建新密钥版本失败: {str(e)}")
            raise
    
    logger.error(f"创建新密钥版本失败，已达到最大重试次数: {str(last_exception)}")
    raise last_exception

def update_db_password_with_retry(current_secret, new_secret):
    retry_count = 0
    last_exception = None
    
    while retry_count < MAX_RETRIES:
        try:
            update_db_password(current_secret, new_secret)
            return
        except Exception as e:
            last_exception = e
            retry_count += 1
            logger.warning(f"更新数据库密码失败, 尝试重试 ({retry_count}/{MAX_RETRIES}): {str(e)}")
            if retry_count < MAX_RETRIES:
                time.sleep(RETRY_DELAY_SECONDS)
    
    logger.error(f"更新数据库密码失败, 已达到最大重试次数: {str(last_exception)}")
    raise last_exception

def update_db_password(current_secret, new_secret):
    conn = None
    try:
        conn = pymysql.connect(
            host=current_secret['host'],
            user=current_secret['username'],
            password=current_secret['password'],
            port=int(current_secret.get('port', 3306)),
            connect_timeout=10
        )
        
        with conn.cursor() as cur:
            escaped_password = new_secret['password'].replace("'", "''")
            
            sql = f"ALTER USER '{current_secret['username']}'@'%' IDENTIFIED BY '{escaped_password}'"
            cur.execute(sql)
            conn.commit()
            
            logger.info("等待密码更改复制到全球数据库的只读集群...")
            time.sleep(10)
            
    except pymysql.MySQLError as e:
        logger.error(f"更新数据库密码失败: {str(e)}")
        raise
    finally:
        if conn:
            conn.close()

def test_db_connection_with_retry(secret):
    retry_count = 0
    last_exception = None
    
    while retry_count < MAX_RETRIES:
        try:
            test_db_connection(secret)
            return
        except Exception as e:
            last_exception = e
            retry_count += 1
            logger.warning(f"测试数据库连接失败, 尝试重试 ({retry_count}/{MAX_RETRIES}): {str(e)}")
            if retry_count < MAX_RETRIES:
                time.sleep(RETRY_DELAY_SECONDS)
    
    logger.error(f"测试数据库连接失败, 已达到最大重试次数: {str(last_exception)}")
    raise last_exception

def test_db_connection(secret):
    conn = None
    try:
        conn = pymysql.connect(
            host=secret['host'],
            user=secret['username'],
            password=secret['password'],
            port=int(secret.get('port', 3306)),
            connect_timeout=10
        )
        
        with conn.cursor() as cur:
            cur.execute("SELECT 1")
            result = cur.fetchone()
            if result[0] != 1:
                raise Exception("数据库连接测试失败")
                
    except pymysql.MySQLError as e:
        logger.error(f"测试数据库连接失败: {str(e)}")
        raise
    finally:
        if conn:
            conn.close()