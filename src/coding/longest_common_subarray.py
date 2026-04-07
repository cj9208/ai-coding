from functools import lru_cache


def longest_common_subarray_dp(A, B):
    m, n = len(A), len(B)
    # Initialize a 2D table with zeros
    dp = [[0] * (n + 1) for _ in range(m + 1)]
    max_len = 0

    for i in range(1, m + 1):
        for j in range(1, n + 1):
            if A[i - 1] == B[j - 1]:
                dp[i][j] = dp[i - 1][j - 1] + 1
                max_len = max(max_len, dp[i][j])
            else:
                # Contiguous requirement: reset to 0 if no match
                dp[i][j] = 0

    return max_len


def longest_common_subarray(A, B):

    @lru_cache(None)
    def solve(i, j):
        if i < 0 or j < 0:
            # (ending_here, overall_max)
            return 0, 0

        # Get results from sub-problems
        _, max_left = solve(i - 1, j)
        _, max_up = solve(i, j - 1)

        # Calculate match ending exactly here
        ending_here = (1 + solve(i - 1, j - 1)[0]) if A[i] == B[j] else 0

        # The new overall max is the best of all three directions
        overall_max = max(ending_here, max_left, max_up)

        return ending_here, overall_max

    return solve(len(A) - 1, len(B) - 1)[1]


def longest_common_subarray_suffix_array(A, B):
    """
    Optimized solution using Suffix Array concepts.
    Time Complexity: O((m + n) * log(m + n)) due to sorting suffixes.
    Space Complexity: O(m + n)

    This approach constructs suffixes for both arrays, sorts them,
    and finds the longest common prefix between adjacent suffixes
    from different original arrays.

    The current implementation for comparing two suffixes is O(k)
    where k is the length of the common prefix, which can lead to
    O((m + n) * min(m, n)) in the worst case. This can be optimized,
    but it's too complex to optimize further.
    """
    m, n = len(A), len(B)
    if m == 0 or n == 0:
        return 0

    # Create suffixes with indices indicating origin (0 for A, 1 for B)
    # Format: (suffix_tuple, origin_index, start_index)
    suffixes = []
    for i in range(m):
        suffixes.append((tuple(A[i:]), 0, i))
    for j in range(n):
        suffixes.append((tuple(B[j:]), 1, j))

    # Sort suffixes lexicographically
    suffixes.sort(key=lambda x: x[0])

    max_len = 0
    # Compare adjacent suffixes in the sorted list
    for k in range(1, len(suffixes)):
        prev_suffix, prev_origin, _ = suffixes[k - 1]
        curr_suffix, curr_origin, _ = suffixes[k]

        # Only compare if they come from different arrays
        if prev_origin != curr_origin:
            # Calculate longest common prefix
            # Since they are tuples, we can compare directly
            lcp = 0
            min_len = min(len(prev_suffix), len(curr_suffix))
            for x in range(min_len):
                if prev_suffix[x] == curr_suffix[x]:
                    lcp += 1
                else:
                    break
            max_len = max(max_len, lcp)

    return max_len


def longest_common_subarray_dp_efficient(A, B):
    """
    Space-optimized DP solution for Longest Common Subarray.
    Uses only two rows (previous and current) instead of a full 2D table.
    """
    m, n = len(A), len(B)
    # Ensure we iterate over the shorter array for the inner loop to save space if needed,
    # but here we just optimize the rows based on A.
    prev_row = [0] * (n + 1)
    max_len = 0

    for i in range(1, m + 1):
        curr_row = [0] * (n + 1)
        for j in range(1, n + 1):
            if A[i - 1] == B[j - 1]:
                curr_row[j] = prev_row[j - 1] + 1
                max_len = max(max_len, curr_row[j])
            else:
                curr_row[j] = 0
        prev_row = curr_row

    return max_len


def longest_common_subarray_dp_efficient2(A, B):
    """
    Space-optimized DP solution for Longest Common Subarray.
    Uses only one row (1D array) instead of two rows.
    Iterates backwards to ensure we use values from the previous row.
    """
    m, n = len(A), len(B)
    # Ensure we use the shorter dimension for the array size if possible,
    # but standard optimization keeps B's length for the inner loop structure.
    dp = [0] * (n + 1)
    max_len = 0

    for i in range(1, m + 1):
        # Iterate backwards to avoid overwriting dp[j-1]
        # which is needed for dp[j] in the next step
        # dp[j-1] here effectively acts as prev_row[j-1]
        # because it hasn't been updated in this i-loop yet
        for j in range(n, 0, -1):
            if A[i - 1] == B[j - 1]:
                dp[j] = dp[j - 1] + 1
                max_len = max(max_len, dp[j])
            else:
                dp[j] = 0

    return max_len


def longest_common_subarray_binary_search_hash(A, B):
    """
    Optimized solution using Binary Search on Length and Rolling Hash.
    Time Complexity: O((m + n) * log(min(m, n)))
    Space Complexity: O(m + n)

    This approach binary searches the length of the longest common subarray.
    For a given length L, it uses Rabin-Karp hashing to check if there exists
    a subarray of length L in A that is also present in B.

    This method is actually used to deduplicate code/webpage or plagiarism detection
    in a large codebase. It's a bit more complex than the previous solutions,
    but it's a good optimization.
    """
    m, n = len(A), len(B)
    if m == 0 or n == 0:
        return 0

    # Ensure A is the shorter array to minimize hash set size if needed,
    # though logic below handles both.
    # We'll iterate through all possible lengths from 1 to min(m, n)

    def has_common_subarray(length: int) -> bool:
        if length == 0:
            return True

        base = 10**9 + 7
        mod = 2**64  # Use natural overflow for speed, or a large prime

        # Calculate base^length % mod
        power = 1
        for _ in range(length):
            power = (power * base) % mod

        def get_hashes(arr, ll):
            hashes = set()
            h = 0
            # Calculate hash for first window
            for i in range(ll):
                h = (h * base + arr[i]) % mod
            hashes.add(h)

            # Slide window
            for i in range(1, len(arr) - ll + 1):
                # Remove leading element, add trailing element
                h = (h * base - arr[i - 1] * power + arr[i + ll - 1]) % mod
                hashes.add(h)
            return hashes

        hashes_A = get_hashes(A, length)
        hashes_B = get_hashes(B, length)

        # Note: Hash collisions are possible but rare with large mod.
        # For strict correctness, one might verify matches,
        # but for competitive programming/optimization contexts, this is often accepted.
        # To be safer, we can intersect sets.
        return bool(hashes_A & hashes_B)

    low, high = 0, min(m, n)
    ans = 0
    while low <= high:
        mid = (low + high) // 2
        if has_common_subarray(mid):
            ans = mid
            low = mid + 1
        else:
            high = mid - 1

    return ans


class TrieNode:
    def __init__(self):
        self.children = {}
        self.word = None  # Stores the full word at the leaf node


def find_words(board, words):
    # 1. Build the Trie
    root = TrieNode()
    for w in words:
        node = root
        for char in w:
            node = node.children.setdefault(char, TrieNode())
        node.word = w

    rows, cols = len(board), len(board[0])
    results = []

    def backtrack(r, c, parent):
        char = board[r][c]
        curr_node = parent.children[char]

        # Found a word!
        if curr_node.word:
            results.append(curr_node.word)
            curr_node.word = None  # Avoid duplicates

        # Mark as visited
        board[r][c] = "#"

        # Explore neighbors
        for dr, dc in [(0, 1), (0, -1), (1, 0), (-1, 0)]:
            nr, nc = r + dr, c + dc
            if (
                0 <= nr < rows
                and 0 <= nc < cols
                and board[nr][nc] in curr_node.children
            ):
                backtrack(nr, nc, curr_node)

        # Backtrack: Restore the board
        board[r][c] = char

        # Optimization: Prune leaf nodes to speed up the search
        if not curr_node.children:
            parent.children.pop(char)

    # 2. Start DFS from every cell that is a start of any word in Trie
    for r in range(rows):
        for c in range(cols):
            if board[r][c] in root.children:
                backtrack(r, c, root)

    return results


if __name__ == "__main__":
    lst1, lst2 = ["a", "b", "c", "d", "e"], ["a", "b", "c", "d", "e", "f"]
    print(longest_common_subarray(lst1, lst2))
    print(longest_common_subarray_dp(lst1, lst2))
    print(longest_common_subarray_dp_efficient(lst1, lst2))
    print(longest_common_subarray_dp_efficient2(lst1, lst2))
