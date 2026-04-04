def trap_rain_water(height):
    stack = []
    water = 0
    current = 0

    while current < len(height):
        # We found a bar taller than the top of our stack (a potential right wall)
        while stack and height[current] > height[stack[-1]]:
            top = stack.pop()  # This is the "bottom" of our container

            if not stack:
                break  # No left wall, water can't be trapped

            distance = current - stack[-1] - 1
            # The height of the water is the min of the two walls minus the bottom
            bounded_height = min(height[current], height[stack[-1]]) - height[top]

            water += distance * bounded_height

        stack.append(current)
        current += 1

    return water


# Example: [0,1,0,2,1,0,1,3,2,1,2,1] -> 6
if __name__ == "__main__":
    height = [0, 1, 0, 2, 1, 0, 1, 3, 2, 1, 2, 1]
    print(trap_rain_water(height))
