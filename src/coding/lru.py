# build a lru cache using doubly linked list and a map


class Node:
    def __init__(self, key, value):
        self.key = key
        self.value = value
        self.prev = None
        self.next = None


class LRUCache:
    def __init__(self, capacity: int):
        self.capacity = capacity
        self.size = 0
        self.head = Node(None, None)
        self.tail = Node(None, None)
        self.head.next = self.tail
        self.tail.prev = self.head
        self.map: dict = {}

    def _add_to_head(self, node: Node):
        node.prev = self.head
        node.next = self.head.next
        self.head.next.prev = node
        self.head.next = node
        self.size += 1
        return node

    def _remove_node(self, node: Node):
        node.prev.next = node.next
        node.next.prev = node.prev

        self.size -= 1
        return node

    def get(self, key):
        if key not in self.map:
            return -1

        node = self.map[key]

        # fix pre and later nodes
        self._remove_node(node)

        # mv node to head
        self._add_to_head(node)
        return node.value

    def put(self, key, value):
        if key in self.map:

            node = self.map[key]
            # update value
            node.value = value
            self._remove_node(node)

        else:
            # add node to head
            node = Node(key, value)
            self.map[key] = node

        self._add_to_head(node)

        while self.size > self.capacity:
            self._remove_node(self.tail.prev)

        return node

    def pretty_print(self):
        node = self.head.next
        while node != self.tail:
            print(node.key, node.value)
            node = node.next
        print("===")


if __name__ == "__main__":
    cache = LRUCache(5)
    cache.put(1, 1)
    cache.put(2, 2)

    # visualize the cache
    cache.pretty_print()

    print(cache.get(1))  # returns 1
    cache.put(3, 3)  # evicts key 2
    cache.pretty_print()
    print(cache.get(2))  # returns -1 (not found)
    cache.put(4, 4)  # evicts key 1
    cache.pretty_print()
