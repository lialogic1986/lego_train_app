import cv2
import numpy as np

img = np.zeros((300, 500, 3), dtype=np.uint8)
cv2.putText(img, "OpenCV window OK", (30, 160), cv2.FONT_HERSHEY_SIMPLEX, 1, (255,255,255), 2)

cv2.imshow("test", img)
print("Press any key in the window...")
cv2.waitKey(0)
cv2.destroyAllWindows()

