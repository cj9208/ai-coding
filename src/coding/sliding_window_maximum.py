from collections import deque


def maxSlidingWindow(nums, k):
    dq = deque()  # Stores indices
    res = []

    for i in range(len(nums)):
        # 1. Remove indices that are out of the current window range
        if dq and dq[0] < i - k + 1:
            dq.popleft()

        # 2. Remove indices of numbers smaller than the current number
        # (They will never be the maximum in this or future windows)
        while dq and nums[dq[-1]] < nums[i]:
            dq.pop()

        dq.append(i)

        # 3. Once we've hit the window size, the front is our max
        if i >= k - 1:
            res.append(nums[dq[0]])

    return res


def longestSubarray(nums, limit):
    max_dq = deque()  # Decreasing: max_dq[0] is the maximum
    min_dq = deque()  # Increasing: min_dq[0] is the minimum
    left = 0
    res = 0

    for right in range(len(nums)):
        # Maintain max_dq (Monotonic Decreasing)
        while max_dq and nums[max_dq[-1]] <= nums[right]:
            max_dq.pop()
        max_dq.append(right)

        # Maintain min_dq (Monotonic Increasing)
        while min_dq and nums[min_dq[-1]] >= nums[right]:
            min_dq.pop()
        min_dq.append(right)

        # If the window is invalid, shrink from the left
        while nums[max_dq[0]] - nums[min_dq[0]] > limit:
            if max_dq[0] == left:
                max_dq.popleft()
            if min_dq[0] == left:
                min_dq.popleft()
            left += 1

        res = max(res, right - left + 1)

    return res


if __name__ == "__main__":
    print(maxSlidingWindow([1, 3, -1, -3, 5, 3, 6, 7], 3))
