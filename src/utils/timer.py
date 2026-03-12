# a timer decorator to time the execution of a function
# prints the execution time in seconds and the function name
import time
def timer(func):
    def wrapper(*args, **kwargs):
        start_time = time.time()
        result = func(*args, **kwargs)
        end_time = time.time()
        print(f"Execution time for {func.__name__}: {end_time - start_time} seconds")
        return result
    return wrapper