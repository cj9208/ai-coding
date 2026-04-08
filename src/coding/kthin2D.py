# find the kth smallest number in a 2D matrix
# each row and each column is sorted

import heapq


def countLessThan(matrix, mid):
    n = len(matrix)
    i = n - 1
    j = 0
    count = 0
    while i >= 0 and j < n:
        if matrix[i][j] > mid:
            i -= 1
        else:
            count += i + 1
            j += 1
    return count


def kthSmallest(matrix, k):
    n = len(matrix)
    left = matrix[0][0]
    right = matrix[n - 1][n - 1]
    while left < right:
        # Use integer division to ensure mid is an integer
        mid = left + (right - left) // 2
        if countLessThan(matrix, mid) < k:
            left = mid + 1
        else:
            right = mid
    return left


# use heap for kth elment if k is small
# can be exteneded to external sorting for large data that doesn't fit in memory,
# using a min heap to merge k sorted lists (rows of the matrix)


def kthSmallest_heap(matrix, k):
    n = len(matrix)
    heap = []
    for i in range(min(n, k)):
        # the kth smallest number must be in the first k rows
        heapq.heappush(heap, (matrix[i][0], i, 0))

    count = 0
    while heap:
        num, i, j = heapq.heappop(heap)

        if j + 1 < n:
            heapq.heappush(heap, (matrix[i][j + 1], i, j + 1))

        count += 1
        if count == k:
            return num


if __name__ == "__main__":
    matrix = [[1, 5, 9], [10, 11, 14], [12, 13, 15]]
    k = 8
    print(kthSmallest(matrix, k))
