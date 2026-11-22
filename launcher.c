#include <stdio.h>
#include <unistd.h>

int main(int argc, char **argv) {
    (void)argc;
    const char *executable = "/opt/shop/shop-portable";
#if defined(__x86_64__)
    if (__builtin_cpu_supports("avx2") && __builtin_cpu_supports("popcnt"))
        executable = "/opt/shop/shop-avx2";
#endif
    execv(executable, argv);
    perror(executable);
    return 1;
}
