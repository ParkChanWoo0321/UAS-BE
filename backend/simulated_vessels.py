"""Image-only fallback for the two red-hulled vessels in the Gazebo coast world."""

import cv2
import numpy as np


def hull_centres(image):
    hsv = cv2.cvtColor(image, cv2.COLOR_BGR2HSV)
    red = cv2.inRange(hsv, (0, 66, 44), (23, 255, 255))
    red |= cv2.inRange(hsv, (169, 66, 44), (179, 255, 255))
    red = cv2.morphologyEx(red, cv2.MORPH_CLOSE, cv2.getStructuringElement(cv2.MORPH_ELLIPSE, (5, 5)))
    water = cv2.inRange(hsv, (80, 29, 38), (128, 255, 245))
    height, width = red.shape
    centres = []
    for contour in cv2.findContours(red, cv2.RETR_EXTERNAL, cv2.CHAIN_APPROX_SIMPLE)[0]:
        if not 25 <= cv2.contourArea(contour) <= width * height * .14:
            continue
        centre, sides, _ = cv2.minAreaRect(contour)
        if not 1.45 <= max(sides) / max(1, min(sides)) <= 10:
            continue
        x, y, w, h = cv2.boundingRect(contour)
        margin = max(8, min(28, round(max(sides) * .30)))
        if min(x, y, width - x - w, height - y - h) < margin:
            continue
        window = water[y - margin:y + h + margin, x - margin:x + w + margin]
        # Use the water around the hull, independent of the hull colour and angle.
        outside = window.size - w * h
        surrounding = np.count_nonzero(window) - np.count_nonzero(window[margin:margin+h, margin:margin+w])
        if surrounding >= .70 * outside:
            centres.append(centre)
    return centres


class SimulatedVessels:
    def __init__(self):
        self.previous = ()

    def detect(self, image):
        current = hull_centres(image)
        radius_squared = (min(image.shape[:2]) * .23) ** 2
        matches = sum(any((x - px) ** 2 + (y - py) ** 2 <= radius_squared for px, py in self.previous)
                      for x, y in current)
        self.previous = current
        # Zero denotes an image-rule sighting, which has no YOLO confidence.
        return [0.0] * matches
