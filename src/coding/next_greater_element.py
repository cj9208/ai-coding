from typing import List


def next_greater_element(nums1: List[int]) -> list[int]:
    """Find the next greater element for each element in nums1

    Args:
        nums1 (List[int]): list of numbers

    Returns:
        list[int]: list of next greater elements for each element in nums1
    """
    stack: list[int] = []
    result = [-1] * len(nums1)

    for i in range(len(nums1)):
        while stack and nums1[stack[-1]] < nums1[i]:
            result[stack.pop()] = nums1[i]
        stack.append(i)
    return result


if __name__ == "__main__":
    nums1 = [4, 1, 2]

    print(next_greater_element(nums1))

    nums1 = [4, 1, 2, 1, 0, -1, 5, 6]

    print(next_greater_element(nums1))
