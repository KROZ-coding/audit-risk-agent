"""S2 批次目录并发隔离测试

锁定行为：
- 并发线程各自 begin_batch 后，current_batch_stamp 互不串扰（ContextVar 隔离）
- 主上下文 begin_batch 后，普通工作线程（无上下文传播）读取到全局镜像兜底
- reset_batch 清理两种存储
"""
import os
import sys
import threading

sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "src"))

from local_storage import begin_batch, current_batch_stamp, reset_batch


class TestBatchIsolation:
    def test_concurrent_threads_isolated(self):
        """S2 核心场景：两个并发运行各自开启批次，互不串目录"""
        results = {}

        def _worker(name, stamp):
            begin_batch(stamp)
            # 模拟导出耗时窗口内另一线程开启新批次
            barrier.wait(timeout=5)
            results[name] = current_batch_stamp()

        barrier = threading.Barrier(2)
        t1 = threading.Thread(target=_worker, args=("A", "20260101_010001"))
        t2 = threading.Thread(target=_worker, args=("B", "20260101_010002"))
        t1.start(); t2.start()
        t1.join(timeout=10); t2.join(timeout=10)

        assert results["A"] == "20260101_010001", "线程 A 的批次被线程 B 切走（串库）"
        assert results["B"] == "20260101_010002", "线程 B 的批次被线程 A 切走（串库）"
        reset_batch()

    def test_worker_thread_falls_back_to_global_mirror(self):
        """主上下文 begin_batch 后，未传播上下文的工作线程读到全局镜像（兼容旧行为）"""
        reset_batch()
        begin_batch("20260202_020202")
        seen = {}
        t = threading.Thread(target=lambda: seen.update(v=current_batch_stamp()))
        t.start(); t.join(timeout=5)
        assert seen["v"] == "20260202_020202"
        reset_batch()

    def test_auto_init_when_unset(self):
        reset_batch()
        stamp = current_batch_stamp()
        assert len(stamp) == 15 and "_" in stamp
        reset_batch()
