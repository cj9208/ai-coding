# in case the splitted array must be in order with original array


def splitArray(nums, m):
    # 1. Define the search range
    # The smallest possible 'largest sum' is the maximum element in nums
    # The largest possible 'largest sum' is the sum of all elements in nums
    low = max(nums)
    high = sum(nums)
    ans = high

    # Helper function: Can we split 'nums' into 'm' pieces
    # such that no piece has a sum > 'max_sum_limit'?
    def can_split(max_sum_limit):
        subarray_count = 1
        current_sum = 0

        for num in nums:
            if current_sum + num > max_sum_limit:
                # Start a new subarray
                subarray_count += 1
                current_sum = num
                # If we need more than m subarrays, this limit is too small
                if subarray_count > m:
                    return False
            else:
                current_sum += num
        return True

    # 2. Binary Search over the range [low, high]
    while low <= high:
        mid = (low + high) // 2

        if can_split(mid):
            # If 'mid' works, try to find a smaller 'largest sum'
            ans = mid
            high = mid - 1
        else:
            # If 'mid' is too small to fit in m subarrays, increase the limit
            low = mid + 1

    return ans


# Example usage:
# nums = [7, 2, 5, 10, 8], m = 2
# Output: 18 (Subarrays: [7, 2, 5] and [10, 8])


# remove order constraint
def minimizeMaxSum(nums, m):
    # Sort descending: a common heuristic to prune faster
    nums.sort(reverse=True)
    subsets = [0] * m
    ans = sum(nums)

    def backtrack(idx, ans):
        # Base case: all numbers placed
        if idx == len(nums):
            ans = min(ans, max(subsets))
            return

        # Try placing nums[idx] in each of the m subsets
        for i in range(m):
            # Pruning: if current subset sum + current num exceeds our best ans, skip
            if subsets[i] + nums[idx] < ans:

                subsets[i] += nums[idx]
                backtrack(idx + 1)
                subsets[i] -= nums[idx]

            # Optimization: If the subset is empty, don't try other empty subsets
            if subsets[i] == 0:
                break

    backtrack(0, 10000)
    return ans


if __name__ == "__main__":
    nums = [7, 2, 5, 10, 8]
    m = 2
    print(splitArray(nums, m))
