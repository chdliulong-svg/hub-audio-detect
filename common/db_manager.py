# -*- coding: utf-8 -*-
"""
=============================================================================
风电机组碰撞声学诊断系统 · 工业级 SQLite 数据库持久化与自动定时热备份引擎
架构特点:
  1. 零配置内嵌式关系数据库 (SQLite3)，无网络与服务依赖，适应风电工控机封闭环境；
  2. 启用 WAL (Write-Ahead Logging) 预写日志与 NORMAL 同步模式，杜绝异常断电损坏；
  3. 细粒度字段索引 (时间戳、报警状态、仲裁等级、复核状态、RMS幅值) 支持毫秒级海量检索；
  4. 完整遥测字典 (raw_json) 原位无损封存，保障历史诊断结构 100% 向下兼容；
  5. 在线无锁热备份 (Online Hot Backup)，业务无感备份，支持时间戳轮转与超期自动淘汰。
=============================================================================
"""
import os
import sys
import time
import json
import sqlite3
import threading
from datetime import datetime, timedelta

class DatabaseManager:
    """风电音频诊断工业级数据库管理器"""

    def __init__(self, db_path="records/history.db"):
        self.db_path = os.path.abspath(db_path)
        self.db_dir = os.path.dirname(self.db_path)
        os.makedirs(self.db_dir, exist_ok=True)
        self.lock = threading.Lock()
        self._backup_thread = None
        self._stop_backup = threading.Event()
        self.initialize()

    def get_connection(self):
        """获取配置良好且带超时的数据库连接"""
        conn = sqlite3.connect(self.db_path, timeout=15.0)
        conn.row_factory = sqlite3.Row
        return conn

    def initialize(self):
        """初始化数据库表架构、索引及 WAL 模式"""
        with self.lock:
            with self.get_connection() as conn:
                cursor = conn.cursor()
                # 开启 WAL 预写日志模式与安全同步配置 (抗突发掉电与读写并发冲突)
                cursor.execute("PRAGMA journal_mode = WAL;")
                cursor.execute("PRAGMA synchronous = NORMAL;")
                cursor.execute("PRAGMA busy_timeout = 10000;")

                # 创建核心台账表
                cursor.execute("""
                CREATE TABLE IF NOT EXISTS audio_records (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    filename TEXT UNIQUE NOT NULL,
                    timestamp TEXT NOT NULL,
                    duration_sec REAL DEFAULT 20.0,
                    peak_amplitude REAL DEFAULT 0.0,
                    rms_amplitude REAL DEFAULT 0.0,
                    crest_factor REAL DEFAULT 0.0,
                    is_clipped INTEGER DEFAULT 0,
                    rail_voltage REAL DEFAULT 0.0,
                    clip_msg TEXT DEFAULT '',
                    scores_json TEXT DEFAULT '{}',
                    alarms_json TEXT DEFAULT '{}',
                    model_details_json TEXT DEFAULT '{}',
                    alarm_votes INTEGER DEFAULT 0,
                    mean_confidence_pct REAL DEFAULT 0.0,
                    consensus_level TEXT NOT NULL,
                    consensus_title TEXT NOT NULL,
                    consensus_badge TEXT NOT NULL,
                    is_alarm INTEGER DEFAULT 0,
                    infer_ms REAL DEFAULT 0.0,
                    wav_file TEXT DEFAULT '',
                    plot_file TEXT DEFAULT '',
                    review_status TEXT DEFAULT 'PENDING_REVIEW',
                    review_reviewer TEXT DEFAULT '',
                    review_time TEXT DEFAULT '',
                    review_notes TEXT DEFAULT '',
                    raw_json TEXT NOT NULL,
                    created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
                );
                """)

                # 创建高频检索索引
                cursor.execute("CREATE INDEX IF NOT EXISTS idx_records_ts ON audio_records(timestamp);")
                cursor.execute("CREATE INDEX IF NOT EXISTS idx_records_level ON audio_records(consensus_level);")
                cursor.execute("CREATE INDEX IF NOT EXISTS idx_records_votes ON audio_records(alarm_votes);")
                cursor.execute("CREATE INDEX IF NOT EXISTS idx_records_alarm ON audio_records(is_alarm);")
                cursor.execute("CREATE INDEX IF NOT EXISTS idx_records_review ON audio_records(review_status);")
                cursor.execute("CREATE INDEX IF NOT EXISTS idx_records_rms ON audio_records(rms_amplitude);")
                conn.commit()

    @staticmethod
    def _extract_row_data(r):
        """从标准化遥测记录字典中提取数据元组"""
        fn = str(r.get("filename", "")).strip()
        ts = str(r.get("timestamp", "")).strip()
        dur = float(r.get("duration_sec", 20.0) or 20.0)
        peak = float(r.get("peak_amplitude", 0.0) or 0.0)
        rms = float(r.get("rms_amplitude", 0.0) or 0.0)
        crest = float(r.get("crest_factor", 0.0) or 0.0)
        is_clip = 1 if r.get("is_clipped", False) else 0
        rail = float(r.get("rail_voltage", 0.0) or 0.0)
        clip_msg = str(r.get("clip_msg", "") or "")
        
        scores_json = json.dumps(r.get("scores", {}), ensure_ascii=False)
        alarms_json = json.dumps(r.get("alarms", {}), ensure_ascii=False)
        details_json = json.dumps(r.get("model_details", {}), ensure_ascii=False)
        
        votes = int(r.get("alarm_votes", 0) or 0)
        mean_conf = float(r.get("mean_confidence_pct", 0.0) or 0.0)
        level = str(r.get("consensus_level", "NORMAL_CLEAN_NOISE"))
        title = str(r.get("consensus_title", "正常平稳"))
        badge = str(r.get("consensus_badge", "badge-success"))
        is_alarm = 1 if r.get("is_alarm", False) else 0
        infer_ms = float(r.get("infer_ms", 0.0) or 0.0)
        
        wav = str(r.get("wav_file", "") or "")
        plot = str(r.get("plot_file", "") or "")
        
        review = r.get("review") or {}
        rev_status = str(review.get("status", "PENDING_REVIEW") or "PENDING_REVIEW")
        rev_reviewer = str(review.get("reviewer", "") or "")
        rev_time = str(review.get("reviewed_at", "") or review.get("review_time", "") or "")
        rev_notes = str(review.get("notes", "") or "")
        
        raw_json_str = json.dumps(r, ensure_ascii=False)
        
        return (
            fn, ts, dur, peak, rms, crest, is_clip, rail, clip_msg,
            scores_json, alarms_json, details_json, votes, mean_conf,
            level, title, badge, is_alarm, infer_ms, wav, plot,
            rev_status, rev_reviewer, rev_time, rev_notes, raw_json_str
        )

    def insert_or_replace_record(self, record_dict):
        """单例原子插入或更新单条诊断记录"""
        data = self._extract_row_data(record_dict)
        sql = """
        INSERT INTO audio_records (
            filename, timestamp, duration_sec, peak_amplitude, rms_amplitude, crest_factor,
            is_clipped, rail_voltage, clip_msg, scores_json, alarms_json, model_details_json,
            alarm_votes, mean_confidence_pct, consensus_level, consensus_title, consensus_badge,
            is_alarm, infer_ms, wav_file, plot_file, review_status, review_reviewer,
            review_time, review_notes, raw_json
        ) VALUES (
            ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?
        )
        ON CONFLICT(filename) DO UPDATE SET
            timestamp = excluded.timestamp,
            duration_sec = excluded.duration_sec,
            peak_amplitude = excluded.peak_amplitude,
            rms_amplitude = excluded.rms_amplitude,
            crest_factor = excluded.crest_factor,
            is_clipped = excluded.is_clipped,
            rail_voltage = excluded.rail_voltage,
            clip_msg = excluded.clip_msg,
            scores_json = excluded.scores_json,
            alarms_json = excluded.alarms_json,
            model_details_json = excluded.model_details_json,
            alarm_votes = excluded.alarm_votes,
            mean_confidence_pct = excluded.mean_confidence_pct,
            consensus_level = excluded.consensus_level,
            consensus_title = excluded.consensus_title,
            consensus_badge = excluded.consensus_badge,
            is_alarm = excluded.is_alarm,
            infer_ms = excluded.infer_ms,
            wav_file = excluded.wav_file,
            plot_file = excluded.plot_file,
            review_status = excluded.review_status,
            review_reviewer = excluded.review_reviewer,
            review_time = excluded.review_time,
            review_notes = excluded.review_notes,
            raw_json = excluded.raw_json;
        """
        with self.lock:
            with self.get_connection() as conn:
                conn.execute(sql, data)
                conn.commit()

    def batch_insert_records(self, record_dicts):
        """单事务批量高速灌入记录列表 (用于初始化导入与海量归档恢复)"""
        if not record_dicts:
            return 0
        batch_data = [self._extract_row_data(r) for r in record_dicts]
        sql = """
        INSERT INTO audio_records (
            filename, timestamp, duration_sec, peak_amplitude, rms_amplitude, crest_factor,
            is_clipped, rail_voltage, clip_msg, scores_json, alarms_json, model_details_json,
            alarm_votes, mean_confidence_pct, consensus_level, consensus_title, consensus_badge,
            is_alarm, infer_ms, wav_file, plot_file, review_status, review_reviewer,
            review_time, review_notes, raw_json
        ) VALUES (
            ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?
        )
        ON CONFLICT(filename) DO UPDATE SET
            timestamp = excluded.timestamp,
            duration_sec = excluded.duration_sec,
            peak_amplitude = excluded.peak_amplitude,
            rms_amplitude = excluded.rms_amplitude,
            crest_factor = excluded.crest_factor,
            is_clipped = excluded.is_clipped,
            rail_voltage = excluded.rail_voltage,
            clip_msg = excluded.clip_msg,
            scores_json = excluded.scores_json,
            alarms_json = excluded.alarms_json,
            model_details_json = excluded.model_details_json,
            alarm_votes = excluded.alarm_votes,
            mean_confidence_pct = excluded.mean_confidence_pct,
            consensus_level = excluded.consensus_level,
            consensus_title = excluded.consensus_title,
            consensus_badge = excluded.consensus_badge,
            is_alarm = excluded.is_alarm,
            infer_ms = excluded.infer_ms,
            wav_file = excluded.wav_file,
            plot_file = excluded.plot_file,
            review_status = excluded.review_status,
            review_reviewer = excluded.review_reviewer,
            review_time = excluded.review_time,
            review_notes = excluded.review_notes,
            raw_json = excluded.raw_json;
        """
        with self.lock:
            with self.get_connection() as conn:
                conn.executemany(sql, batch_data)
                conn.commit()
        return len(batch_data)

    def get_records_count(self):
        """获取当前数据库中记录总行数"""
        with self.get_connection() as conn:
            cursor = conn.cursor()
            cursor.execute("SELECT COUNT(*) FROM audio_records;")
            return cursor.fetchone()[0]

    def get_record_by_filename(self, filename):
        """根据文件名获取单个记录的完整字典"""
        with self.get_connection() as conn:
            cursor = conn.cursor()
            cursor.execute("SELECT raw_json FROM audio_records WHERE filename = ? LIMIT 1;", (filename,))
            row = cursor.fetchone()
            if row:
                try:
                    return json.loads(row["raw_json"])
                except Exception:
                    return None
            return None

    def get_all_records(self, limit=50000):
        """按时间戳倒序返回完整遥测记录列表 (解析自 raw_json，保障 100% 格式无损)"""
        with self.get_connection() as conn:
            cursor = conn.cursor()
            if limit and limit > 0:
                cursor.execute("SELECT raw_json FROM audio_records ORDER BY timestamp DESC, id DESC LIMIT ?;", (limit,))
            else:
                cursor.execute("SELECT raw_json FROM audio_records ORDER BY timestamp DESC, id DESC;")
            rows = cursor.fetchall()
            records = []
            for row in rows:
                try:
                    records.append(json.loads(row["raw_json"]))
                except Exception:
                    pass
            return records

    def get_latest_record(self):
        """获取最新诊断的一条记录"""
        with self.get_connection() as conn:
            cursor = conn.cursor()
            cursor.execute("SELECT raw_json FROM audio_records ORDER BY timestamp DESC, id DESC LIMIT 1;")
            row = cursor.fetchone()
            if row:
                try:
                    return json.loads(row["raw_json"])
                except Exception:
                    return None
            return None

    def update_review(self, filename, status, reviewer, reviewed_at, notes):
        """更新指定记录的人工复核结论 (事务更新各结构字段与 raw_json 镜像)"""
        with self.lock:
            with self.get_connection() as conn:
                cursor = conn.cursor()
                cursor.execute("SELECT raw_json FROM audio_records WHERE filename = ?;", (filename,))
                row = cursor.fetchone()
                if not row:
                    return False
                try:
                    rec = json.loads(row["raw_json"])
                except Exception:
                    rec = {"filename": filename}
                
                rev_info = {
                    "status": status,
                    "reviewer": reviewer,
                    "reviewed_at": reviewed_at,
                    "notes": notes
                }
                rec["review"] = rev_info
                raw_json_str = json.dumps(rec, ensure_ascii=False)
                
                cursor.execute("""
                UPDATE audio_records SET
                    review_status = ?,
                    review_reviewer = ?,
                    review_time = ?,
                    review_notes = ?,
                    raw_json = ?
                WHERE filename = ?;
                """, (status, reviewer, reviewed_at, notes, raw_json_str, filename))
                conn.commit()
                return True

    def delete_record(self, filename):
        """根据文件名删除指定记录"""
        with self.lock:
            with self.get_connection() as conn:
                cursor = conn.cursor()
                cursor.execute("DELETE FROM audio_records WHERE filename = ?;", (filename,))
                deleted = cursor.rowcount > 0
                conn.commit()
                return deleted

    def clear_all_records(self):
        """清空全量记录"""
        with self.lock:
            with self.get_connection() as conn:
                cursor = conn.cursor()
                cursor.execute("DELETE FROM audio_records;")
                conn.commit()

    def get_kpi_summary(self):
        """超高速聚合大屏 KPI 统计指标 (SQL 聚合运算毫秒级返回)"""
        with self.get_connection() as conn:
            cursor = conn.cursor()
            cursor.execute("""
            SELECT 
                COUNT(*) as total,
                SUM(CASE WHEN alarm_votes = 3 THEN 1 ELSE 0 END) as unanimous,
                SUM(CASE WHEN alarm_votes = 2 AND is_alarm = 1 THEN 1 ELSE 0 END) as majority,
                SUM(CASE WHEN consensus_level = 'SHIELDED_ISOLATED_DISTURBANCE' OR alarm_votes = 1 THEN 1 ELSE 0 END) as shielded,
                SUM(CASE WHEN consensus_level = 'NORMAL_CLEAN_NOISE' THEN 1 ELSE 0 END) as normal,
                SUM(CASE WHEN is_clipped = 1 OR consensus_level = 'HARDWARE_CLIPPING_DISCARD' THEN 1 ELSE 0 END) as clipped,
                SUM(CASE WHEN consensus_level = 'MACHINE_STARTUP_SAFE' THEN 1 ELSE 0 END) as startup,
                SUM(CASE WHEN consensus_level = 'MACHINE_SHUTDOWN_SAFE' THEN 1 ELSE 0 END) as shutdown,
                SUM(CASE WHEN review_status != 'PENDING_REVIEW' AND review_status != 'UNREVIEWED' THEN 1 ELSE 0 END) as reviewed,
                SUM(CASE WHEN review_status = 'CONFIRMED_COLLISION' THEN 1 ELSE 0 END) as review_collision,
                SUM(CASE WHEN review_status = 'FALSE_ALARM_INTERCEPT' THEN 1 ELSE 0 END) as review_false_alarm
            FROM audio_records;
            """)
            r = cursor.fetchone()
            return {
                "total": r["total"] or 0,
                "unanimous": r["unanimous"] or 0,
                "majority": r["majority"] or 0,
                "shielded": r["shielded"] or 0,
                "normal": r["normal"] or 0,
                "clipped": r["clipped"] or 0,
                "startup": r["startup"] or 0,
                "shutdown": r["shutdown"] or 0,
                "reviewed": r["reviewed"] or 0,
                "review_collision": r["review_collision"] or 0,
                "review_false_alarm": r["review_false_alarm"] or 0
            }

    # ---------------------------------------------------------------------------
    # 迁移与冷启动初始化
    # ---------------------------------------------------------------------------
    def migrate_from_json(self, json_path, rename_backup=True):
        """
        从原有的 JSON 文件全量迁移至 SQLite 数据库
        返回成功迁移的记录条数
        """
        if not os.path.exists(json_path) or os.path.getsize(json_path) < 10:
            return 0
        try:
            with open(json_path, "r", encoding="utf-8") as f:
                records = json.load(f)
                if not isinstance(records, list) or not records:
                    return 0
                count = self.batch_insert_records(records)
                
                # 迁移成功后无损更名备份原 JSON 文件
                if rename_backup and count > 0:
                    backup_name = json_path + ".migrated_backup"
                    try:
                        if os.path.exists(backup_name):
                            os.remove(backup_name)
                        os.replace(json_path, backup_name)
                    except Exception:
                        pass
                return count
        except Exception as e:
            return 0

    # ---------------------------------------------------------------------------
    # 在线热备份引擎 (Online Hot Backup)
    # ---------------------------------------------------------------------------
    def backup_database(self, backup_dir=None, retention_days=30):
        """
        执行 SQLite 原生无锁在线热备份 (Online Backup API)
        即使主库正在高并发读写，也能生成一致性物理镜像，绝不阻塞诊断推理。
        自动按保留天数轮转清理过期备份。
        """
        if not backup_dir:
            backup_dir = os.path.join(self.db_dir, "backups")
        os.makedirs(backup_dir, exist_ok=True)

        now_str = datetime.now().strftime("%Y%m%d_%H%M%S")
        backup_filename = f"history_{now_str}.db"
        target_path = os.path.join(backup_dir, backup_filename)
        tmp_target = target_path + ".tmp"

        try:
            with self.get_connection() as src_conn:
                dst_conn = sqlite3.connect(tmp_target)
                with dst_conn:
                    # 原生热备份：按页流式复制
                    src_conn.backup(dst_conn, pages=100)
                dst_conn.close()

            if os.path.exists(tmp_target):
                os.replace(tmp_target, target_path)

            # 轮转清理超期备份文件
            self._cleanup_old_backups(backup_dir, retention_days=retention_days)
            size_mb = os.path.getsize(target_path) / (1024 * 1024)
            return True, target_path, f"备份成功 (大小: {size_mb:.2f} MB)"
        except Exception as e:
            if os.path.exists(tmp_target):
                try:
                    os.remove(tmp_target)
                except Exception:
                    pass
            return False, "", f"备份失败: {e}"

    def _cleanup_old_backups(self, backup_dir, retention_days=30):
        """删除超过保留天数的陈旧备份文件"""
        if retention_days <= 0 or not os.path.isdir(backup_dir):
            return
        cutoff_time = time.time() - (retention_days * 86400)
        try:
            for f in os.listdir(backup_dir):
                if f.startswith("history_") and f.endswith(".db"):
                    p = os.path.join(backup_dir, f)
                    if os.path.isfile(p) and os.path.getmtime(p) < cutoff_time:
                        try:
                            os.remove(p)
                        except Exception:
                            pass
        except Exception:
            pass

    def list_backups(self, backup_dir=None):
        """获取当前所有本地备份清单"""
        if not backup_dir:
            backup_dir = os.path.join(self.db_dir, "backups")
        if not os.path.isdir(backup_dir):
            return []
        backups = []
        try:
            for f in os.listdir(backup_dir):
                if f.startswith("history_") and f.endswith(".db"):
                    p = os.path.join(backup_dir, f)
                    if os.path.isfile(p):
                        mtime = os.path.getmtime(p)
                        sz = os.path.getsize(p)
                        backups.append({
                            "filename": f,
                            "path": p,
                            "size_bytes": sz,
                            "size_mb": round(sz / (1024 * 1024), 2),
                            "created_at": datetime.fromtimestamp(mtime).strftime("%Y-%m-%d %H:%M:%S"),
                            "mtime": mtime
                        })
            backups.sort(key=lambda x: x["mtime"], reverse=True)
        except Exception:
            pass
        return backups

    def start_auto_backup_worker(self, interval_hours=24, retention_days=30, log_callback=None):
        """启动后台独立定时热备份守护线程 (启动时若无备份先执行基线快照)"""
        if self._backup_thread and self._backup_thread.is_alive():
            return

        # 启动后若尚无任何备份，先同步执行一次初始基线备份
        backups = self.list_backups()
        if not backups:
            ok, path, msg = self.backup_database(retention_days=retention_days)
            if log_callback:
                log_callback(f"[💾 数据库热备引擎] 初始基线快照备份完成: {msg}")

        def _worker():
            interval_sec = max(3600, int(interval_hours * 3600))
            if log_callback:
                log_callback(f"[💾 数据库热备引擎] 自动定时热备守护已就绪 (每 {interval_hours} 小时在线热备一次，保留 {retention_days} 天)")

            while not self._stop_backup.is_set():
                for _ in range(int(interval_sec / 10)):
                    if self._stop_backup.is_set():
                        break
                    time.sleep(10)
                if self._stop_backup.is_set():
                    break
                
                # 执行定时热备份
                ok, path, msg = self.backup_database(retention_days=retention_days)
                if log_callback:
                    lvl = "INFO" if ok else "WARN"
                    log_callback(f"[💾 数据库热备引擎] 定时自动热备完成: {msg}")

        self._stop_backup.clear()
        self._backup_thread = threading.Thread(target=_worker, daemon=True, name="db_backup_worker")
        self._backup_thread.start()

    def stop_auto_backup_worker(self):
        """停止定时备份线程"""
        if self._backup_thread and self._backup_thread.is_alive():
            self._stop_backup.set()
