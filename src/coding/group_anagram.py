# group a list of words by anagrams
from collections import defaultdict


def group_anagrams(words):
    """Group anagrams together"""
    anagram_groups = {}
    for word in words:
        sorted_word = "".join(sorted(word))
        if sorted_word in anagram_groups:
            anagram_groups[sorted_word].append(word)
        else:
            anagram_groups[sorted_word] = [word]
    return list(anagram_groups.values())


def group_anagrams_bucket_sort(words):
    """Group anagrams together using bucket sort"""

    ans = defaultdict(list)
    for s in words:
        count = [0] * 26  # Fixed size for lowercase English letters
        for char in s:
            count[ord(char) - ord("a")] += 1
        # Tuples are hashable and can be used as keys
        ans[tuple(count)].append(s)
    return list(ans.values())


# complexity analysis
# sort: klog k
# bucket sort: k
# where k is longest length of words
# thus if k is large, bucket sort is faster

if __name__ == "__main__":
    words = ["eat", "tea", "tan", "ate", "nat", "bat"]
    print(group_anagrams(words))
    print(group_anagrams_bucket_sort(words))
