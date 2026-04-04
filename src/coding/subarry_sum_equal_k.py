from typing import List

# Brute force method, calculate lst[i:j] for all I, j
# An optimization, sliding window, or two pointer, i=0, j=1,
# we move right pointer if the sum is bigger than k or move left pointer if sum is less than k.
# The complexity is O(n). The question here is it only works for non-negative arrays


# find the number of subarrays with sum equal to k
def subarraySum(nums: List[int], k: int) -> int:
    count = 0
    current_sum = 0
    # map stores prefix_sum -> number of times it occurred
    prefix_sums = {0: 1}

    for n in nums:
        current_sum += n
        diff = current_sum - k

        # If current_sum - k exists, it means a valid subarray exists
        if diff in prefix_sums:
            count += prefix_sums[diff]

        prefix_sums[current_sum] = prefix_sums.get(current_sum, 0) + 1

    return count


if __name__ == "__main__":
    nums = [1, 2, 3, 4, 5, 6, 7, 8, 9, 10]
    k = 15
    print(subarraySum(nums, k))
