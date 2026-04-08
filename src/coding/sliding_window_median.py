import heapq
from collections import defaultdict


class MedianFinder:
    def __init__(self):
        self.small = []  # Max-heap (negative values)
        self.large = []  # Min-heap

    def addNum(self, num: int) -> None:
        # 1. Push to small heap (Max-heap)
        heapq.heappush(self.small, -num)

        # 2. Balance: Move the largest of 'small' to 'large'
        heapq.heappush(self.large, -heapq.heappop(self.small))

        # 3. Keep 'small' size >= 'large' size
        if len(self.large) > len(self.small):
            heapq.heappush(self.small, -heapq.heappop(self.large))

    def findMedian(self) -> float:
        if len(self.small) > len(self.large):
            return float(-self.small[0])
        return (-self.small[0] + self.large[0]) / 2.0


# key point is that we only care about the median, so if the element to be deleted
# is not the top of the heap, we can just mark it as "delayed"
# and remove it lazily when it reaches the top


class SlidingWindowMedian:
    def __init__(self, k):
        self.k = k
        self.small = []  # Max-heap (stores negative values)
        self.large = []  # Min-heap
        self.delayed = defaultdict(int)  # For lazy deletion: {val: count}
        self.small_size = 0
        self.large_size = 0

    def _clean_heap(self, heap, is_small):
        """Removes elements from the top if they are marked for deletion."""
        while heap:
            val = -heap[0] if is_small else heap[0]
            if self.delayed[val] > 0:
                self.delayed[val] -= 1
                heapq.heappop(heap)
            else:
                break

    def _rebalance(self):
        """Ensures heaps are balanced: small_size == large_size or small_size == large_size + 1"""
        if self.small_size > self.large_size + 1:
            # Move from small to large
            val = -heapq.heappop(self.small)
            heapq.heappush(self.large, val)
            self.small_size -= 1
            self.large_size += 1
            self._clean_heap(self.small, True)
        elif self.small_size < self.large_size:
            # Move from large to small
            val = heapq.heappop(self.large)
            heapq.heappush(self.small, -val)
            self.large_size -= 1
            self.small_size += 1
            self._clean_heap(self.large, False)

    def add(self, num):
        if not self.small or num <= -self.small[0]:
            heapq.heappush(self.small, -num)
            self.small_size += 1
        else:
            heapq.heappush(self.large, num)
            self.large_size += 1
        self._rebalance()

    def remove(self, num):
        self.delayed[num] += 1
        if num <= -self.small[0]:
            self.small_size -= 1
            if num == -self.small[0]:
                self._clean_heap(self.small, True)
        else:
            self.large_size -= 1
            if num == self.large[0]:
                self._clean_heap(self.large, False)
        self._rebalance()

    def get_median(self):
        if self.k % 2 == 1:
            return float(-self.small[0])
        return (-self.small[0] + self.large[0]) / 2.0


def medianSlidingWindow(nums, k):
    finder = SlidingWindowMedian(k)
    res = []

    # Initialize the first window
    for i in range(k):
        finder.add(nums[i])
    res.append(finder.get_median())

    # Slide the window
    for i in range(k, len(nums)):
        finder.add(nums[i])
        finder.remove(nums[i - k])
        res.append(finder.get_median())

    return res
