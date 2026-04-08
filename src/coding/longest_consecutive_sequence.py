# the key point, we only need to check the number that appears in the begin of a sequence
# the judgement: if n - 1 not in nums, then n is the begin of a sequence


def longest_consecutive_sequence(nums):
    nums = set(nums)
    longest = 0
    for n in nums:
        if n - 1 not in nums:
            length = 1
            while n + length in nums:
                length += 1
            longest = max(length, longest)
    return longest


# for stream data, use union find algorithm
class UnionFind:
    def __init__(self):
        # parent[i] stores the representative of the set containing i
        self.parent = {}
        # size[i] stores the size of the set where i is the representative
        self.size = {}
        self.max_size = 0

    def add(self, n):
        if n in self.parent:
            return
        self.parent[n] = n
        self.size[n] = 1
        self.max_size = max(self.max_size, 1)

    def find(self, i):
        # Path compression: makes the tree flat for O(alpha(n)) speed
        if self.parent[i] == i:
            return i
        self.parent[i] = self.find(self.parent[i])
        return self.parent[i]

    def union(self, i, j):
        root_i = self.find(i)
        root_j = self.find(j)

        if root_i != root_j:
            # Union by size: attach smaller tree to larger tree
            if self.size[root_i] < self.size[root_j]:
                root_i, root_j = root_j, root_i

            self.parent[root_j] = root_i
            self.size[root_i] += self.size[root_j]
            self.max_size = max(self.max_size, self.size[root_i])


def longestConsecutive(nums):
    if not nums:
        return 0
    uf = UnionFind()
    num_set = set()  # To quickly check if neighbors exist

    for n in nums:
        if n in num_set:
            continue
        num_set.add(n)
        uf.add(n)

        # Look for neighbors to merge
        if n - 1 in num_set:
            uf.union(n, n - 1)
        if n + 1 in num_set:
            uf.union(n, n + 1)

    return uf.max_size


if __name__ == "__main__":
    lst = [100, 4, 200, 1, 3, 2]
    print(longest_consecutive_sequence(lst))
    print(longestConsecutive(lst))

    lst1 = [0, 3, 7, 2, 5, 8, 4, 6, 0, 1]
    print(longest_consecutive_sequence(lst1))
    print(longestConsecutive(lst1))
