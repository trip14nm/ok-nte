import importlib
import unittest
from types import SimpleNamespace
from unittest.mock import patch

import numpy as np

vision_module = importlib.import_module("src.tasks.mixin.VisionMixin")
VisionMixin = vision_module.VisionMixin


class _FakeSift:
    def detectAndCompute(self, _gray, _mask):
        keypoints = [vision_module.cv2.KeyPoint(float(index), float(index), 1) for index in range(4)]
        descriptors = np.zeros((4, 128), dtype=np.float32)
        return keypoints, descriptors


class _FakeMatcher:
    def knnMatch(self, _template_descriptors, _scene_descriptors, k=2):
        return [
            [
                SimpleNamespace(queryIdx=index, trainIdx=index, distance=0.1),
                SimpleNamespace(queryIdx=index, trainIdx=index, distance=1.0),
            ]
            for index in range(3)
        ]


class _VisionTask(VisionMixin):
    @property
    def frame(self):
        return self._frame


class TestVisionMixin(unittest.TestCase):
    def test_sift_homography_requires_at_least_four_matches(self):
        task = object.__new__(_VisionTask)
        task._frame = np.zeros((32, 32, 3), dtype=np.uint8)
        task.get_original_feature_by_name = lambda _name: SimpleNamespace(
            mat=np.zeros((16, 16, 3), dtype=np.uint8)
        )
        task.draw_boxes = lambda *args, **kwargs: None
        task.log_debug = lambda _message: None

        with (
            patch.object(vision_module.cv2, "SIFT_create", return_value=_FakeSift()),
            patch.object(vision_module.cv2, "BFMatcher", return_value=_FakeMatcher()),
            patch.object(vision_module.cv2, "findHomography") as find_homography,
        ):
            result = task.find_sift_feature(
                "unit_sift_min_matches",
                min_match_count=3,
                small_target_retry=False,
            )

        self.assertIsNone(result)
        find_homography.assert_not_called()


if __name__ == "__main__":
    unittest.main()
