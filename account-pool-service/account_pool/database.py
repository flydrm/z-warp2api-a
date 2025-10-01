#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
账号数据库管理模块
使用SQLite存储和管理Warp账号信息
"""

import sqlite3
import threading
import time
from contextlib import contextmanager
from typing import List, Dict, Optional, Any
from dataclasses import dataclass
from datetime import datetime

from config import config
from utils.logger import logger


@dataclass
class Account:
    """账号数据模型"""
    id: Optional[int] = None
    email: str = ""
    local_id: str = ""  # Firebase用户ID，也是Warp UID
    id_token: str = ""
    refresh_token: str = ""
    status: str = "available"  # available, in_use, expired, error
    created_at: Optional[datetime] = None
    last_used: Optional[datetime] = None
    last_refresh_time: Optional[datetime] = None  # 上次刷新token的时间
    use_count: int = 0
    session_id: Optional[str] = None  # 用于并发请求的会话标识
    
    def to_dict(self) -> Dict[str, Any]:
        """转换为字典"""
        return {
            'id': self.id,
            'email': self.email,
            'local_id': self.local_id,  # 这个就是Warp UID
            'id_token': self.id_token,
            'refresh_token': self.refresh_token,
            'status': self.status,
            'created_at': self.created_at.isoformat() if self.created_at else None,
            'last_used': self.last_used.isoformat() if self.last_used else None,
            'last_refresh_time': self.last_refresh_time.isoformat() if self.last_refresh_time else None,
            'use_count': self.use_count,
            'session_id': self.session_id
        }


class AccountDatabase:
    """账号数据库管理器"""
    
    def __init__(self, db_path: str = None):
        """初始化数据库"""
        self.db_path = db_path or config.DATABASE_PATH
        self._local = threading.local()
        self._init_database()
        logger.info(f"账号数据库初始化完成: {self.db_path}")
    
    def _get_connection(self) -> sqlite3.Connection:
        """获取线程本地的数据库连接"""
        if not hasattr(self._local, 'connection'):
            self._local.connection = sqlite3.connect(
                self.db_path, 
                check_same_thread=False,
                timeout=30.0
            )
            self._local.connection.row_factory = sqlite3.Row
        return self._local.connection
    
    @contextmanager
    def _get_cursor(self):
        """获取数据库游标的上下文管理器"""
        conn = self._get_connection()
        cursor = conn.cursor()
        try:
            yield cursor
        finally:
            cursor.close()
    
    def _init_database(self):
        """初始化数据库表结构"""
        with self._get_cursor() as cursor:
            cursor.execute('''
                CREATE TABLE IF NOT EXISTS accounts (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    email TEXT UNIQUE NOT NULL,
                    local_id TEXT NOT NULL,
                    id_token TEXT NOT NULL,
                    refresh_token TEXT NOT NULL,
                    status TEXT DEFAULT 'available',
                    created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
                    last_used TIMESTAMP,
                    last_refresh_time TIMESTAMP,
                    use_count INTEGER DEFAULT 0,
                    session_id TEXT
                )
            ''')
            
            # 检查并添加缺失的列（用于数据库升级）
            cursor.execute("PRAGMA table_info(accounts)")
            columns = [column[1] for column in cursor.fetchall()]
            
            if 'last_refresh_time' not in columns:
                cursor.execute('ALTER TABLE accounts ADD COLUMN last_refresh_time TIMESTAMP')
                logger.info("数据库升级：添加last_refresh_time字段")
            
            # 创建索引优化查询性能
            cursor.execute('CREATE INDEX IF NOT EXISTS idx_status ON accounts (status)')
            cursor.execute('CREATE INDEX IF NOT EXISTS idx_session_id ON accounts (session_id)')
            cursor.execute('CREATE INDEX IF NOT EXISTS idx_last_used ON accounts (last_used)')
            
            cursor.connection.commit()
    
    def add_account(self, account: Account) -> bool:
        """添加账号到数据库"""
        try:
            with self._get_cursor() as cursor:
                cursor.execute('''
                    INSERT INTO accounts (email, local_id, id_token, refresh_token, status, created_at)
                    VALUES (?, ?, ?, ?, ?, ?)
                ''', (
                    account.email,
                    account.local_id,
                    account.id_token,
                    account.refresh_token,
                    account.status,
                    datetime.now()
                ))
                cursor.connection.commit()
                logger.info(f"成功添加账号到数据库: {account.email}")
                return True
        except sqlite3.IntegrityError as e:
            logger.warning(f"账号已存在，跳过添加: {account.email}")
            return False
        except Exception as e:
            logger.error(f"添加账号失败: {e}")
            return False
    
    def get_available_accounts(self, limit: int = None) -> List[Account]:
        """获取可用账号列表"""
        with self._get_cursor() as cursor:
            query = '''
                SELECT * FROM accounts 
                WHERE status = 'available' 
                ORDER BY last_used ASC NULLS FIRST, created_at ASC
            '''
            if limit:
                query += f' LIMIT {limit}'
            
            cursor.execute(query)
            rows = cursor.fetchall()
            
            accounts = []
            for row in rows:
                account = Account(
                    id=row['id'],
                    email=row['email'],
                    local_id=row['local_id'],
                    id_token=row['id_token'],
                    refresh_token=row['refresh_token'],
                    status=row['status'],
                    created_at=datetime.fromisoformat(row['created_at']) if row['created_at'] else None,
                    last_used=datetime.fromisoformat(row['last_used']) if row['last_used'] else None,
                    last_refresh_time=datetime.fromisoformat(row['last_refresh_time']) if row['last_refresh_time'] else None,
                    use_count=row['use_count'] or 0,
                    session_id=row['session_id']
                )
                accounts.append(account)
            
            return accounts
    
    def allocate_accounts_for_session(self, session_id: str, count: int = None) -> List[Account]:
        """为会话分配指定数量的账号"""
        if count is None:
            count = config.ACCOUNTS_PER_REQUEST
        
        try:
            with self._get_cursor() as cursor:
                # 使用事务确保原子性
                cursor.execute('BEGIN TRANSACTION')
                
                # 选择可用账号
                cursor.execute('''
                    SELECT id FROM accounts 
                    WHERE status = 'available' 
                    ORDER BY last_used ASC NULLS FIRST, created_at ASC
                    LIMIT ?
                ''', (count,))
                
                account_ids = [row[0] for row in cursor.fetchall()]
                
                if len(account_ids) < count:
                    cursor.execute('ROLLBACK')
                    logger.warning(f"可用账号不足，需要 {count} 个，实际可用 {len(account_ids)} 个")
                    return []
                
                # 更新账号状态为使用中
                for account_id in account_ids:
                    cursor.execute('''
                        UPDATE accounts 
                        SET status = 'in_use', session_id = ?, last_used = ?, use_count = use_count + 1
                        WHERE id = ?
                    ''', (session_id, datetime.now(), account_id))
                
                cursor.execute('COMMIT')
                
                # 获取分配的账号详细信息
                placeholders = ','.join(['?'] * len(account_ids))
                cursor.execute(f'''
                    SELECT * FROM accounts WHERE id IN ({placeholders})
                ''', account_ids)
                
                rows = cursor.fetchall()
                accounts = []
                for row in rows:
                    account = Account(
                        id=row['id'],
                        email=row['email'],
                        local_id=row['local_id'],
                        id_token=row['id_token'],
                        refresh_token=row['refresh_token'],
                        status=row['status'],
                        created_at=datetime.fromisoformat(row['created_at']) if row['created_at'] else None,
                        last_used=datetime.fromisoformat(row['last_used']) if row['last_used'] else None,
                        last_refresh_time=datetime.fromisoformat(row['last_refresh_time']) if row['last_refresh_time'] else None,
                        use_count=row['use_count'] or 0,
                        session_id=row['session_id']
                    )
                    accounts.append(account)
                
                logger.info(f"成功为会话 {session_id} 分配 {len(accounts)} 个账号")
                return accounts
                
        except Exception as e:
            logger.error(f"分配账号失败: {e}")
            return []
    
    def release_accounts_for_session(self, session_id: str) -> bool:
        """释放会话的所有账号"""
        try:
            with self._get_cursor() as cursor:
                cursor.execute('''
                    UPDATE accounts 
                    SET status = 'available', session_id = NULL 
                    WHERE session_id = ?
                ''', (session_id,))
                
                affected_rows = cursor.rowcount
                cursor.connection.commit()
                
                logger.info(f"成功释放会话 {session_id} 的 {affected_rows} 个账号")
                return True
        except Exception as e:
            logger.error(f"释放账号失败: {e}")
            return False
    
    def update_account_token(self, email: str, id_token: str, refresh_token: str, refresh_time: datetime = None) -> bool:
        """更新账号的token信息和刷新时间"""
        try:
            with self._get_cursor() as cursor:
                if refresh_time is None:
                    refresh_time = datetime.now()
                
                cursor.execute('''
                    UPDATE accounts 
                    SET id_token = ?, refresh_token = ?, last_refresh_time = ?
                    WHERE email = ?
                ''', (id_token, refresh_token, refresh_time, email))
                
                cursor.connection.commit()
                logger.info(f"成功更新账号token: {email}")
                return cursor.rowcount > 0
        except Exception as e:
            logger.error(f"更新账号token失败: {e}")
            return False
    
    def mark_account_expired(self, email: str) -> bool:
        """标记账号为过期状态"""
        try:
            with self._get_cursor() as cursor:
                cursor.execute('''
                    UPDATE accounts 
                    SET status = 'expired', session_id = NULL 
                    WHERE email = ?
                ''', (email,))
                
                cursor.connection.commit()
                logger.info(f"账号标记为过期: {email}")
                return cursor.rowcount > 0
        except Exception as e:
            logger.error(f"标记账号过期失败: {e}")
            return False
    
    def mark_account_as_expired(self, email: str) -> bool:
        """标记账号为过期状态（别名方法）"""
        return self.mark_account_expired(email)
    
    def get_pool_statistics(self) -> Dict[str, int]:
        """获取账号池统计信息"""
        with self._get_cursor() as cursor:
            stats = {}
            
            # 各状态账号数量
            cursor.execute('SELECT status, COUNT(*) FROM accounts GROUP BY status')
            for row in cursor.fetchall():
                stats[row[0]] = row[1]
            
            # 总账号数
            cursor.execute('SELECT COUNT(*) FROM accounts')
            stats['total'] = cursor.fetchone()[0]
            
            # 活跃会话数
            cursor.execute('SELECT COUNT(DISTINCT session_id) FROM accounts WHERE session_id IS NOT NULL')
            stats['active_sessions'] = cursor.fetchone()[0]
            
            return stats
    
    def cleanup_expired_accounts(self) -> int:
        """清理过期账号"""
        try:
            with self._get_cursor() as cursor:
                cursor.execute('DELETE FROM accounts WHERE status = "expired"')
                deleted_count = cursor.rowcount
                cursor.connection.commit()
                
                logger.info(f"清理了 {deleted_count} 个过期账号")
                return deleted_count
        except Exception as e:
            logger.error(f"清理过期账号失败: {e}")
            return 0
    
    def can_refresh_token(self, email: str, min_interval_hours: int = 1) -> tuple[bool, Optional[str]]:
        """
        检查账号是否可以刷新token（严格遵守时间限制）
        
        Args:
            email: 账号邮箱
            min_interval_hours: 最小刷新间隔（小时）
            
        Returns:
            (can_refresh, error_message)
        """
        try:
            with self._get_cursor() as cursor:
                cursor.execute(
                    'SELECT last_refresh_time FROM accounts WHERE email = ?',
                    (email,)
                )
                
                result = cursor.fetchone()
                if not result:
                    return False, f"账号不存在: {email}"
                
                last_refresh_time = result['last_refresh_time']
                
                # 如果从未刷新过，允许刷新
                if not last_refresh_time:
                    return True, None
                
                # 检查时间间隔
                last_refresh = datetime.fromisoformat(last_refresh_time)
                now = datetime.now()
                time_elapsed = now - last_refresh
                
                min_interval = min_interval_hours * 3600  # 转为秒
                
                if time_elapsed.total_seconds() >= min_interval:
                    return True, None
                else:
                    remaining_seconds = min_interval - time_elapsed.total_seconds()
                    remaining_minutes = int(remaining_seconds // 60)
                    remaining_secs = int(remaining_seconds % 60)
                    return False, f"需要等待 {remaining_minutes} 分 {remaining_secs} 秒后才能刷新"
                    
        except Exception as e:
            logger.error(f"检查刷新权限失败: {e}")
            return False, f"检查失败: {e}"
    
    def get_account_by_email(self, email: str) -> Optional[Account]:
        """根据邮箱获取账号信息"""
        try:
            with self._get_cursor() as cursor:
                cursor.execute('SELECT * FROM accounts WHERE email = ?', (email,))
                row = cursor.fetchone()
                
                if not row:
                    return None
                
                return Account(
                    id=row['id'],
                    email=row['email'],
                    local_id=row['local_id'],
                    id_token=row['id_token'],
                    refresh_token=row['refresh_token'],
                    status=row['status'],
                    created_at=datetime.fromisoformat(row['created_at']) if row['created_at'] else None,
                    last_used=datetime.fromisoformat(row['last_used']) if row['last_used'] else None,
                    last_refresh_time=datetime.fromisoformat(row['last_refresh_time']) if row['last_refresh_time'] else None,
                    use_count=row['use_count'] or 0,
                    session_id=row['session_id']
                )
        except Exception as e:
            logger.error(f"获取账号信息失败: {e}")
            return None
    
    def close(self):
        """关闭数据库连接"""
        if hasattr(self._local, 'connection'):
            self._local.connection.close()
            delattr(self._local, 'connection')


# 全局数据库实例
_db_instance = None


def get_database() -> AccountDatabase:
    """获取数据库实例（单例模式）"""
    global _db_instance
    if _db_instance is None:
        _db_instance = AccountDatabase()
    return _db_instance