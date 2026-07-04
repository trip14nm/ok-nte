import sys
import threading
import time
import unittest
from pathlib import Path

import numpy as np

from ok import Box
from src.YOLO26OpenVINOAsyncDetector import YOLO26OpenVINOAsyncDetector

REPO_ROOT = Path(__file__).resolve().parents[1]
if str(REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(REPO_ROOT))


class FakeRequest:
    def __init__(self):
        self.cancelled = False

    def cancel(self):
        self.cancelled = True


class FakeQueue:
    def __init__(self, ready=True, complete_immediately=False, detections=None):
        self.ready = ready
        self.complete_immediately = complete_immediately
        self.detections = detections or []
        self.callback = None
        self.requests = [FakeRequest()]
        self.started = []
        self.waited = False

    def __iter__(self):
        return iter(self.requests)

    def is_ready(self):
        return self.ready

    def set_callback(self, callback):
        self.callback = callback

    def start_async(self, inputs, user_data):
        self.started.append((inputs, user_data))
        self.ready = False
        if self.complete_immediately and self.callback is not None:
            self.callback(FakeInferRequest(self.detections), user_data)
            self.ready = True

    def wait_all(self):
        self.waited = True


class FakeOutputTensor:
    def __init__(self, data):
        self.data = data


class FakeInferRequest:
    def __init__(self, detections):
        self._detections = detections

    def get_output_tensor(self):
        return FakeOutputTensor(np.array([self._detections], dtype=np.float32))


class TestYOLO26OpenVINOAsyncDetector(unittest.TestCase):
    def _detector(self, queues):
        detector = YOLO26OpenVINOAsyncDetector.__new__(YOLO26OpenVINOAsyncDetector)
        detector.num_requests = 1
        detector._state_lock = threading.RLock()
        detector._retired_infer_queues = []
        detector._active_queue_jobs = {}
        detector.latest_results = ["old"]
        detector.latest_image = None
        detector.class_names = ["target"]
        detector.latency = 0.0
        detector.job_id = 0
        detector._force_next_submit = False
        detector.model_h = 896
        detector.model_w = 1536
        detector.model_ratio = detector.model_w / detector.model_h
        detector.infer_queue = queues.pop(0)

        def create_queue():
            queue = queues.pop(0)
            queue.set_callback(detector._callback)
            return queue

        detector._create_infer_queue = create_queue
        return detector

    def test_detect_sync_rotates_busy_queue_and_waits_for_latest_frame(self):
        old_queue = FakeQueue(ready=False)
        new_queue = FakeQueue(
            ready=True,
            complete_immediately=True,
            detections=[[0, 0, 1536, 896, 0.99, 0]],
        )
        detector = self._detector([old_queue, new_queue])
        image = np.zeros((20, 20, 3), dtype=np.uint8)

        result = detector.detect_sync(image)

        self.assertTrue(old_queue.requests[0].cancelled)
        self.assertIs(detector.infer_queue, new_queue)
        self.assertEqual(len(new_queue.started), 1)
        self.assertEqual(len(result), 1)
        self.assertIsInstance(result[0], Box)
        self.assertEqual(result[0].name, "target")
        self.assertIs(detector.latest_image, image)

    def test_force_rotates_busy_queue_and_submits_latest_frame(self):
        old_queue = FakeQueue(ready=False)
        new_queue = FakeQueue(ready=True)
        detector = self._detector([old_queue, new_queue])
        image = np.zeros((20, 20, 3), dtype=np.uint8)

        result = detector.detect(image, force=True)

        self.assertEqual(result, ["old"])
        self.assertTrue(old_queue.requests[0].cancelled)
        self.assertIs(detector.infer_queue, new_queue)
        self.assertEqual(len(new_queue.started), 1)
        self.assertEqual(detector._get_active_retired_count(), 0)

    def test_clear_cache_makes_next_detect_submit_without_force(self):
        old_queue = FakeQueue(ready=False)
        first_new_queue = FakeQueue(ready=False)
        submitted_queue = FakeQueue(ready=True)
        detector = self._detector([old_queue, first_new_queue, submitted_queue])
        detector._mark_queue_job_started(old_queue)
        image = np.zeros((20, 20, 3), dtype=np.uint8)

        detector.clear_cache()
        result = detector.detect(image)

        self.assertIsNone(result)
        self.assertTrue(old_queue.requests[0].cancelled)
        self.assertTrue(first_new_queue.requests[0].cancelled)
        self.assertIs(detector.infer_queue, submitted_queue)
        self.assertEqual(len(submitted_queue.started), 1)
        self.assertFalse(detector._force_next_submit)

    def test_stale_callback_cannot_overwrite_latest_results(self):
        old_queue = FakeQueue(ready=False)
        new_queue = FakeQueue(ready=True)
        detector = self._detector([old_queue, new_queue])
        image = np.zeros((20, 20, 3), dtype=np.uint8)
        detector.detect(image, force=True)
        current_job_id = detector.job_id

        detector.latest_results = ["newer"]
        detector._callback(
            FakeInferRequest([[0, 0, 10, 10, 0.99, 0]]),
            {
                "box": Box(x=0, y=0, width=20, height=20),
                "threshold": 0.5,
                "label": "target",
                "start_time": time.time(),
                "pad_x": 0,
                "pad_y": 0,
                "target_w": detector.model_w,
                "job_id": current_job_id - 1,
                "queue_id": id(old_queue),
                "image": image,
            },
        )

        self.assertEqual(detector.latest_results, ["newer"])

    def test_cancelled_retired_queue_releases_active_job_immediately(self):
        old_queue = FakeQueue(ready=False)
        detector = self._detector([old_queue])
        detector._mark_queue_job_started(old_queue)

        detector._retire_queue(old_queue, cancel=True)

        self.assertEqual(detector._get_active_retired_count(), 0)
        self.assertNotIn(id(old_queue), detector._active_queue_jobs)
        self.assertTrue(old_queue.requests[0].cancelled)
        self.assertEqual(detector._retired_infer_queues[0]["queue"], old_queue)


if __name__ == "__main__":
    unittest.main()
